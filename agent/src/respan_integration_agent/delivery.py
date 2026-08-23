"""Strict, credential-free contracts for prepared v0b delivery.

This module deliberately contains no Git or HTTP implementation.  It defines the
immutable values that cross the prepare/verify/deliver boundary and the small durable
journal used to recover remote identifiers after process loss.  Patch bytes remain on
the prepared value only; they are never serialized by the recovery journal.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import ClassVar
from urllib.parse import parse_qsl, urlsplit


PREPARED_DELIVERY_SCHEMA = "respan-v0b-prepared-delivery/v1"
VERIFICATION_RECEIPT_SCHEMA = "respan-v0b-verification-receipt/v1"
DELIVERY_MANIFEST_SCHEMA = "respan-v0b-delivery-manifest/v1"
DELIVERY_JOURNAL_SCHEMA = "respan-v0b-delivery-journal/v1"
BACKEND_TRACE_EVIDENCE_SCHEMA = "respan-v0-backend-trace-gate/v1"

_GITHUB_ORIGIN = "https://github.com"
_TRACE_ORIGIN = "https://platform.respan.ai"
_MAX_PATCH_BYTES = 16 * 1024 * 1024
_MAX_CHANGED_PATHS = 512
_MAX_JOURNAL_BYTES = 64 * 1024
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9.][A-Za-z0-9._/-]{0,511}$")
_SAFE_STAGE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ERROR_CODE_RE = re.compile(r"^G_[A-Z][A-Z0-9_]{0,47}$")
_CREDENTIAL_LIKE_RE = re.compile(
    r"(?i)(?:(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
)
_CREDENTIAL_LIKE_BYTES_RE = re.compile(
    rb"(?i)(?:(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}|"
    rb"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
)
_PRODUCTS = frozenset({"tracing", "gateway", "both"})

REQUIRED_VERIFICATION_CHECKS = tuple(
    sorted(
        {
            "backend-agent-trace",
            "backend-target-trace",
            "fresh-target-install",
            "gateway-readiness",
            "semantic-verification",
            "target-runtime",
        }
    )
)


def _require_exact_string(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} contains a control character")
    if _CREDENTIAL_LIKE_RE.search(value) is not None:
        raise ValueError(f"{field_name} contains a credential-like value")
    return value


def _require_sha1(value: object, field_name: str) -> str:
    candidate = _require_exact_string(value, field_name)
    if _SHA1_RE.fullmatch(candidate) is None or int(candidate, 16) == 0:
        raise ValueError(f"{field_name} must be a nonzero lowercase Git SHA")
    return candidate


def _require_sha256(value: object, field_name: str) -> str:
    candidate = _require_exact_string(value, field_name)
    if _SHA256_RE.fullmatch(candidate) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return candidate


def _require_trace_id(value: object, field_name: str) -> str:
    candidate = _require_exact_string(value, field_name)
    if _TRACE_ID_RE.fullmatch(candidate) is None or int(candidate, 16) == 0:
        raise ValueError(f"{field_name} must be a nonzero lowercase trace ID")
    return candidate


def _require_run_id(value: object, field_name: str) -> str:
    candidate = _require_exact_string(value, field_name)
    if _RUN_ID_RE.fullmatch(candidate) is None:
        raise ValueError(f"{field_name} has an invalid value")
    return candidate


def _require_owner(value: object, field_name: str = "owner") -> str:
    candidate = _require_exact_string(value, field_name)
    if _OWNER_RE.fullmatch(candidate) is None or "--" in candidate:
        raise ValueError(f"{field_name} has an invalid value")
    return candidate


def _require_repo(value: object, field_name: str = "repo") -> str:
    candidate = _require_exact_string(value, field_name)
    if _REPO_RE.fullmatch(candidate) is None or candidate.endswith(".git"):
        raise ValueError(f"{field_name} has an invalid value")
    return candidate


def _parse_slug(value: object, field_name: str = "repository") -> tuple[str, str]:
    candidate = _require_exact_string(value, field_name)
    if candidate.count("/") != 1:
        raise ValueError(f"{field_name} must be an owner/repository slug")
    owner, repo = candidate.split("/", 1)
    _require_owner(owner, f"{field_name} owner")
    _require_repo(repo, f"{field_name} repo")
    return owner, repo


def _require_branch_ref(value: object, field_name: str) -> str:
    candidate = _require_exact_string(value, field_name)
    if len(candidate.encode("utf-8")) > 200:
        raise ValueError(f"{field_name} is too long")
    if (
        candidate.startswith(("-", ".", "/"))
        or candidate.endswith((".", "/"))
        or candidate.startswith("refs/")
        or ".." in candidate
        or "//" in candidate
        or "@{" in candidate
        or "\\" in candidate
        or any(character in candidate for character in " ~^:?*[]")
    ):
        raise ValueError(f"{field_name} is not a safe branch ref")
    components = candidate.split("/")
    if any(
        not component
        or component in {".", ".."}
        or component.startswith(".")
        or component.endswith(".lock")
        for component in components
    ):
        raise ValueError(f"{field_name} is not a safe branch ref")
    return candidate


def _require_changed_paths(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not 1 <= len(value) <= _MAX_CHANGED_PATHS:
        raise ValueError("changed_paths must be a non-empty bounded tuple")
    paths: list[str] = []
    for raw_path in value:
        path_text = _require_exact_string(raw_path, "changed path")
        if _SAFE_PATH_RE.fullmatch(path_text) is None or "\\" in path_text:
            raise ValueError("changed path has an invalid value")
        path = PurePosixPath(path_text)
        lowered_parts = tuple(part.lower() for part in path.parts)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or ".git" in lowered_parts
            or lowered_parts[:2] == (".github", "workflows")
        ):
            raise ValueError("changed path is unsafe")
        paths.append(path_text)
    canonical = tuple(sorted(paths))
    if tuple(paths) != canonical or len(set(paths)) != len(paths):
        raise ValueError("changed_paths must be sorted and unique")
    return canonical


def _require_trace_url(value: object, trace_id: str, field_name: str) -> str:
    candidate = _require_exact_string(value, field_name)
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "platform.respan.ai"
        or parsed.path != "/platform/traces"
        or parsed.fragment
        or parse_qsl(parsed.query, keep_blank_values=True)
        != [("trace_unique_id", trace_id)]
        or candidate != f"{_TRACE_ORIGIN}/platform/traces?trace_unique_id={trace_id}"
    ):
        raise ValueError(f"{field_name} is not the canonical trace URL")
    return candidate


def _require_pr_number(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise ValueError("pr_number must be a positive bounded integer")
    return value


def _require_pr_url(value: object, repository: str, pr_number: int) -> str:
    candidate = _require_exact_string(value, "pr_url")
    expected = f"{_GITHUB_ORIGIN}/{repository}/pull/{pr_number}"
    if candidate != expected:
        raise ValueError("pr_url is not the canonical pull-request URL")
    return candidate


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RepositoryTarget:
    """One exact, explicitly authorized public GitHub.com fixture target.

    This value proves only syntactic identity and allowlisting.  The delivery client
    must separately prove that the repository and base are publicly readable without
    presenting a credential.
    """

    repo_url: str
    base_ref: str
    allowed_slug: str
    owner: str = field(init=False)
    repo: str = field(init=False)
    slug: str = field(init=False)
    canonical_url: str = field(init=False)

    def __post_init__(self) -> None:
        repo_url = _require_exact_string(self.repo_url, "repo_url")
        allowed_owner, allowed_repo = _parse_slug(self.allowed_slug, "allowed_slug")
        parsed = urlsplit(repo_url)
        if (
            not repo_url.startswith(f"{_GITHUB_ORIGIN}/")
            or parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.query
            or parsed.fragment
            or "%" in repo_url
            or "\\" in repo_url
        ):
            raise ValueError("repo_url must use the exact public GitHub.com origin")
        parts = parsed.path.split("/")
        if len(parts) != 3 or parts[0] != "" or not parts[1] or not parts[2]:
            raise ValueError("repo_url must contain exactly one owner and repository")
        owner = _require_owner(parts[1])
        raw_repo = parts[2]
        repo = raw_repo[:-4] if raw_repo.endswith(".git") else raw_repo
        _require_repo(repo)
        slug = f"{owner}/{repo}"
        if slug != f"{allowed_owner}/{allowed_repo}" or self.allowed_slug != slug:
            raise ValueError("repo_url does not match the exact allowed_slug")
        base_ref = _require_branch_ref(self.base_ref, "base_ref")
        object.__setattr__(self, "base_ref", base_ref)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "repo", repo)
        object.__setattr__(self, "slug", slug)
        object.__setattr__(self, "canonical_url", f"{_GITHUB_ORIGIN}/{slug}")


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    """An accepted patch and its deterministic, credential-free delivery identity."""

    target: RepositoryTarget
    base_sha: str
    patch: bytes = field(repr=False, hash=False)
    changed_paths: tuple[str, ...]
    product: str
    config_fingerprint: str
    agent_run_id: str
    agent_trace_id: str
    agent_trace_url: str
    patch_sha256: str = field(init=False)
    delivery_fingerprint: str = field(init=False)
    branch: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target, RepositoryTarget):
            raise ValueError("target must be a RepositoryTarget")
        base_sha = _require_sha1(self.base_sha, "base_sha")
        if (
            type(self.patch) is not bytes
            or not 1 <= len(self.patch) <= _MAX_PATCH_BYTES
        ):
            raise ValueError("patch must be non-empty bounded bytes")
        if not self.patch.startswith(b"diff --git "):
            raise ValueError("patch must be a canonical Git binary diff")
        if _CREDENTIAL_LIKE_BYTES_RE.search(self.patch) is not None:
            raise ValueError("patch contains a credential-like value")
        changed_paths = _require_changed_paths(self.changed_paths)
        product = _require_exact_string(self.product, "product")
        if product not in _PRODUCTS:
            raise ValueError("product has an unsupported value")
        config_fingerprint = _require_sha256(
            self.config_fingerprint, "config_fingerprint"
        )
        agent_run_id = _require_run_id(self.agent_run_id, "agent_run_id")
        agent_trace_id = _require_trace_id(self.agent_trace_id, "agent_trace_id")
        agent_trace_url = _require_trace_url(
            self.agent_trace_url, agent_trace_id, "agent_trace_url"
        )
        patch_sha256 = hashlib.sha256(self.patch).hexdigest()
        identity = {
            "base_ref": self.target.base_ref,
            "base_sha": base_sha,
            "changed_paths": list(changed_paths),
            "config_fingerprint": config_fingerprint,
            "patch_sha256": patch_sha256,
            "product": product,
            "repository": self.target.slug,
            "schema_version": PREPARED_DELIVERY_SCHEMA,
        }
        delivery_fingerprint = hashlib.sha256(_canonical_json(identity)).hexdigest()
        branch = f"respan/onboard-{product}-{base_sha[:8]}-{patch_sha256[:12]}"
        _require_branch_ref(branch, "branch")

        object.__setattr__(self, "base_sha", base_sha)
        object.__setattr__(self, "changed_paths", changed_paths)
        object.__setattr__(self, "product", product)
        object.__setattr__(self, "config_fingerprint", config_fingerprint)
        object.__setattr__(self, "agent_run_id", agent_run_id)
        object.__setattr__(self, "agent_trace_id", agent_trace_id)
        object.__setattr__(self, "agent_trace_url", agent_trace_url)
        object.__setattr__(self, "patch_sha256", patch_sha256)
        object.__setattr__(self, "delivery_fingerprint", delivery_fingerprint)
        object.__setattr__(self, "branch", branch)

    def safe_identity_dict(self) -> dict[str, object]:
        """Serialize identifiers only; patch bytes never cross this boundary."""

        return {
            "schema_version": PREPARED_DELIVERY_SCHEMA,
            "repository": self.target.slug,
            "repo_url": self.target.canonical_url,
            "base_ref": self.target.base_ref,
            "base_sha": self.base_sha,
            "product": self.product,
            "config_fingerprint": self.config_fingerprint,
            "patch_sha256": self.patch_sha256,
            "changed_paths": list(self.changed_paths),
            "agent_run_id": self.agent_run_id,
            "agent_trace_id": self.agent_trace_id,
            "agent_trace_url": self.agent_trace_url,
            "delivery_fingerprint": self.delivery_fingerprint,
            "branch": self.branch,
        }


@dataclass(frozen=True, slots=True)
class VerificationReceipt:
    """Evidence that every gate required before GitHub mutation passed."""

    delivery_fingerprint: str
    patch_sha256: str
    changed_paths: tuple[str, ...]
    gateway_report_fingerprint: str
    agent_run_id: str
    agent_trace_id: str
    agent_trace_url: str
    target_run_id: str
    target_trace_id: str
    target_trace_url: str
    backend_verified_at: datetime
    backend_evidence_schema: str
    passed_checks: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.delivery_fingerprint, "delivery_fingerprint")
        _require_sha256(self.patch_sha256, "patch_sha256")
        changed_paths = _require_changed_paths(self.changed_paths)
        _require_sha256(self.gateway_report_fingerprint, "gateway_report_fingerprint")
        agent_run_id = _require_run_id(self.agent_run_id, "agent_run_id")
        agent_trace_id = _require_trace_id(self.agent_trace_id, "agent_trace_id")
        _require_trace_url(self.agent_trace_url, agent_trace_id, "agent_trace_url")
        target_run_id = _require_run_id(self.target_run_id, "target_run_id")
        if target_run_id == agent_run_id:
            raise ValueError("agent and target run IDs must differ")
        target_trace_id = _require_trace_id(self.target_trace_id, "target_trace_id")
        if target_trace_id == agent_trace_id:
            raise ValueError("agent and target trace IDs must differ")
        _require_trace_url(self.target_trace_url, target_trace_id, "target_trace_url")
        if type(self.backend_verified_at) is not datetime:
            raise ValueError("backend_verified_at must be a timezone-aware datetime")
        offset = self.backend_verified_at.utcoffset()
        if offset is None or offset != timedelta(0):
            raise ValueError("backend_verified_at must use UTC")
        if self.backend_evidence_schema != BACKEND_TRACE_EVIDENCE_SCHEMA:
            raise ValueError("backend_evidence_schema has an unsupported value")
        if type(self.passed_checks) is not tuple or (
            self.passed_checks != REQUIRED_VERIFICATION_CHECKS
        ):
            raise ValueError("passed_checks must contain every required check exactly")
        object.__setattr__(self, "changed_paths", changed_paths)

    @classmethod
    def for_prepared(
        cls,
        prepared: PreparedDelivery,
        *,
        gateway_report_fingerprint: str,
        target_run_id: str,
        target_trace_id: str,
        target_trace_url: str,
        backend_verified_at: datetime,
        backend_evidence_schema: str = BACKEND_TRACE_EVIDENCE_SCHEMA,
        passed_checks: tuple[str, ...] = REQUIRED_VERIFICATION_CHECKS,
    ) -> VerificationReceipt:
        if not isinstance(prepared, PreparedDelivery):
            raise ValueError("prepared must be a PreparedDelivery")
        return cls(
            delivery_fingerprint=prepared.delivery_fingerprint,
            patch_sha256=prepared.patch_sha256,
            changed_paths=prepared.changed_paths,
            gateway_report_fingerprint=gateway_report_fingerprint,
            agent_run_id=prepared.agent_run_id,
            agent_trace_id=prepared.agent_trace_id,
            agent_trace_url=prepared.agent_trace_url,
            target_run_id=target_run_id,
            target_trace_id=target_trace_id,
            target_trace_url=target_trace_url,
            backend_verified_at=backend_verified_at,
            backend_evidence_schema=backend_evidence_schema,
            passed_checks=passed_checks,
        )

    def validate_for(self, prepared: PreparedDelivery) -> None:
        if not isinstance(prepared, PreparedDelivery) or (
            self.delivery_fingerprint != prepared.delivery_fingerprint
            or self.patch_sha256 != prepared.patch_sha256
            or self.changed_paths != prepared.changed_paths
            or self.agent_run_id != prepared.agent_run_id
            or self.agent_trace_id != prepared.agent_trace_id
            or self.agent_trace_url != prepared.agent_trace_url
        ):
            raise DeliveryIntegrityError("receipt_validation")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": VERIFICATION_RECEIPT_SCHEMA,
            "delivery_fingerprint": self.delivery_fingerprint,
            "patch_sha256": self.patch_sha256,
            "changed_paths": list(self.changed_paths),
            "gateway_report_fingerprint": self.gateway_report_fingerprint,
            "agent_run_id": self.agent_run_id,
            "agent_trace_id": self.agent_trace_id,
            "agent_trace_url": self.agent_trace_url,
            "target_run_id": self.target_run_id,
            "target_trace_id": self.target_trace_id,
            "target_trace_url": self.target_trace_url,
            "backend_verified_at": self.backend_verified_at.isoformat(),
            "backend_evidence_schema": self.backend_evidence_schema,
            "passed_checks": list(self.passed_checks),
        }


class RemoteDisposition(str, Enum):
    created = "created"
    reused = "reused"


@dataclass(frozen=True, slots=True)
class DeliveryManifest:
    """Sanitized completed-delivery evidence returned to the runner and CLI."""

    target: RepositoryTarget
    base_sha: str
    branch: str
    commit_sha: str
    delivery_fingerprint: str
    pr_number: int
    pr_url: str
    branch_disposition: RemoteDisposition
    pr_disposition: RemoteDisposition

    def __post_init__(self) -> None:
        if not isinstance(self.target, RepositoryTarget):
            raise ValueError("target must be a RepositoryTarget")
        _require_sha1(self.base_sha, "base_sha")
        _require_branch_ref(self.branch, "branch")
        _require_sha1(self.commit_sha, "commit_sha")
        _require_sha256(self.delivery_fingerprint, "delivery_fingerprint")
        pr_number = _require_pr_number(self.pr_number)
        _require_pr_url(self.pr_url, self.target.slug, pr_number)
        if not isinstance(self.branch_disposition, RemoteDisposition):
            raise ValueError("branch_disposition must be a RemoteDisposition")
        if not isinstance(self.pr_disposition, RemoteDisposition):
            raise ValueError("pr_disposition must be a RemoteDisposition")

    def validate_for(
        self, prepared: PreparedDelivery, receipt: VerificationReceipt
    ) -> None:
        receipt.validate_for(prepared)
        if (
            self.target != prepared.target
            or self.base_sha != prepared.base_sha
            or self.branch != prepared.branch
            or self.delivery_fingerprint != prepared.delivery_fingerprint
        ):
            raise DeliveryIntegrityError("manifest_validation")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DELIVERY_MANIFEST_SCHEMA,
            "repository": self.target.slug,
            "base_ref": self.target.base_ref,
            "base_sha": self.base_sha,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "delivery_fingerprint": self.delivery_fingerprint,
            "branch_disposition": self.branch_disposition.value,
            "pr_disposition": self.pr_disposition.value,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
        }


_RECOVERY_KEYS = frozenset(
    {
        "repository",
        "base_ref",
        "base_sha",
        "branch",
        "commit_sha",
        "remote_sha",
        "delivery_fingerprint",
        "phase",
        "pr_number",
        "pr_url",
    }
)


def _validated_recovery(
    recovery: Mapping[str, object] | None,
) -> MappingProxyType[str, object]:
    if recovery is None:
        return MappingProxyType({})
    if not isinstance(recovery, Mapping) or not set(recovery) <= _RECOVERY_KEYS:
        raise ValueError("recovery contains unsupported fields")
    result: dict[str, object] = {}
    repository: str | None = None
    pr_number: int | None = None
    for key, value in recovery.items():
        if key == "repository":
            _parse_slug(value)
            repository = str(value)
        elif key in {"base_ref", "branch"}:
            result[key] = _require_branch_ref(value, key)
            continue
        elif key in {"base_sha", "commit_sha", "remote_sha"}:
            result[key] = _require_sha1(value, key)
            continue
        elif key == "delivery_fingerprint":
            result[key] = _require_sha256(value, key)
            continue
        elif key == "phase":
            try:
                result[key] = DeliveryPhase(value).value
            except (TypeError, ValueError):
                raise ValueError("phase has an invalid value") from None
            continue
        elif key == "pr_number":
            pr_number = _require_pr_number(value)
            result[key] = pr_number
            continue
        elif key == "pr_url":
            result[key] = _require_exact_string(value, key)
            continue
        result[key] = value
    if "pr_url" in result:
        if repository is None or pr_number is None:
            raise ValueError("pr_url recovery requires repository and pr_number")
        _require_pr_url(result["pr_url"], repository, pr_number)
    return MappingProxyType(dict(sorted(result.items())))


class DeliveryError(RuntimeError):
    """A redacted delivery failure with a stable machine-readable contract."""

    code: ClassVar[str] = "G_DELIVERY"
    transient: ClassVar[bool] = False

    def __init__(
        self,
        stage: str,
        *,
        status_code: int | None = None,
        attempts: int | None = None,
        recovery: Mapping[str, object] | None = None,
    ) -> None:
        if _ERROR_CODE_RE.fullmatch(self.code) is None:
            raise ValueError("delivery error code is invalid")
        if type(stage) is not str or _SAFE_STAGE_RE.fullmatch(stage) is None:
            raise ValueError("delivery error stage is invalid")
        if status_code is not None and (
            type(status_code) is not int or not 100 <= status_code <= 599
        ):
            raise ValueError("status_code is invalid")
        if attempts is not None and (
            type(attempts) is not int or not 1 <= attempts <= 100
        ):
            raise ValueError("attempts is invalid")
        self.stage = stage
        self.status_code = status_code
        self.attempts = attempts
        self.recovery = _validated_recovery(recovery)
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"{self.code}: {stage} failed{suffix}")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "stage": self.stage,
            "transient": self.transient,
        }
        if self.status_code is not None:
            result["status_code"] = self.status_code
        if self.attempts is not None:
            result["attempts"] = self.attempts
        if self.recovery:
            result["recovery"] = dict(self.recovery)
        return result


class DeliveryConfigurationError(DeliveryError):
    code = "G_CONFIG"


class DeliveryIntegrityError(DeliveryError):
    code = "G_INTEGRITY"

    _STAGE_CODES: ClassVar[dict[str, str]] = {
        "base_changed": "G_BASE_MOVED",
        "branch_collision": "G_BRANCH_COLLISION",
        "github_closed_pr": "G_PR_CONFLICT",
        "github_duplicate_pr": "G_PR_CONFLICT",
        "github_head_collision": "G_PR_CONFLICT",
        "github_pagination": "G_RESPONSE_SCHEMA",
        "github_pr_content": "G_PR_CONFLICT",
        "github_retry_header": "G_RESPONSE_SCHEMA",
        "github_schema": "G_RESPONSE_SCHEMA",
        "late_worktree_change": "G_PATCH_MISMATCH",
        "patch_identity": "G_PATCH_MISMATCH",
        "workflow_change": "G_PATCH_MISMATCH",
    }

    def __init__(
        self,
        stage: str,
        *,
        status_code: int | None = None,
        attempts: int | None = None,
        recovery: Mapping[str, object] | None = None,
    ) -> None:
        self.code = self._STAGE_CODES.get(stage, type(self).code)
        super().__init__(
            stage,
            status_code=status_code,
            attempts=attempts,
            recovery=recovery,
        )


class DeliveryAuthenticationError(DeliveryError):
    code = "G_AUTH"


class DeliveryAuthorizationError(DeliveryError):
    code = "G_FORBIDDEN"


class DeliveryRepositoryNotFoundError(DeliveryError):
    code = "G_REPOSITORY_NOT_FOUND"


class DeliveryRateLimitError(DeliveryError):
    code = "G_RATE_LIMIT"
    transient = True


class DeliveryTransportError(DeliveryError):
    code = "G_TRANSPORT"
    transient = True


class DeliveryResponseSchemaError(DeliveryError):
    code = "G_RESPONSE_SCHEMA"


class DeliveryPullRequestConflictError(DeliveryError):
    code = "G_PR_CONFLICT"


class DeliveryBranchCollisionError(DeliveryError):
    code = "G_BRANCH_COLLISION"


class DeliveryJournalError(DeliveryError):
    code = "G_JOURNAL"


class DeliveryRecoveryRequired(DeliveryError):
    code = "G_RECOVERY_REQUIRED"
    transient = True


class DeliveryPhase(str, Enum):
    prepared = "prepared"
    committed = "committed"
    branch_observed = "branch_observed"
    branch_pushed = "branch_pushed"
    pr_observed = "pr_observed"
    pr_created = "pr_created"
    completed = "completed"


_PHASE_RANK = {phase: rank for rank, phase in enumerate(DeliveryPhase)}


@dataclass(frozen=True, slots=True)
class DeliveryJournalRecord:
    """One bounded, source-free durable delivery recovery snapshot."""

    phase: DeliveryPhase
    repository: str
    base_ref: str
    base_sha: str
    branch: str
    delivery_fingerprint: str
    commit_sha: str | None = None
    remote_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, DeliveryPhase):
            raise ValueError("phase must be a DeliveryPhase")
        _parse_slug(self.repository)
        _require_branch_ref(self.base_ref, "base_ref")
        _require_sha1(self.base_sha, "base_sha")
        _require_branch_ref(self.branch, "branch")
        _require_sha256(self.delivery_fingerprint, "delivery_fingerprint")
        if self.commit_sha is not None:
            _require_sha1(self.commit_sha, "commit_sha")
        if self.remote_sha is not None:
            _require_sha1(self.remote_sha, "remote_sha")
        if (self.pr_number is None) != (self.pr_url is None):
            raise ValueError("pr_number and pr_url must be supplied together")
        if self.pr_number is not None:
            pr_number = _require_pr_number(self.pr_number)
            assert self.pr_url is not None
            _require_pr_url(self.pr_url, self.repository, pr_number)
        rank = _PHASE_RANK[self.phase]
        if rank >= _PHASE_RANK[DeliveryPhase.committed] and self.commit_sha is None:
            raise ValueError("committed and later phases require commit_sha")
        if rank >= _PHASE_RANK[DeliveryPhase.branch_pushed] and (
            self.remote_sha is None
        ):
            raise ValueError("branch_pushed and later phases require remote_sha")
        if rank >= _PHASE_RANK[DeliveryPhase.pr_created] and self.pr_number is None:
            raise ValueError("pr_created and completed phases require PR identity")

    @classmethod
    def for_prepared(cls, prepared: PreparedDelivery) -> DeliveryJournalRecord:
        if not isinstance(prepared, PreparedDelivery):
            raise ValueError("prepared must be a PreparedDelivery")
        return cls(
            phase=DeliveryPhase.prepared,
            repository=prepared.target.slug,
            base_ref=prepared.target.base_ref,
            base_sha=prepared.base_sha,
            branch=prepared.branch,
            delivery_fingerprint=prepared.delivery_fingerprint,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DeliveryJournalRecord:
        if not isinstance(value, Mapping):
            raise ValueError("journal root must be an object")
        required = {
            "schema_version",
            "phase",
            "repository",
            "base_ref",
            "base_sha",
            "branch",
            "delivery_fingerprint",
        }
        optional = {"commit_sha", "remote_sha", "pr_number", "pr_url"}
        if set(value) - required - optional or not required <= set(value):
            raise ValueError("journal fields do not match the schema")
        if value["schema_version"] != DELIVERY_JOURNAL_SCHEMA:
            raise ValueError("journal schema_version is unsupported")
        try:
            phase = DeliveryPhase(value["phase"])
        except (TypeError, ValueError):
            raise ValueError("journal phase is invalid") from None
        return cls(
            phase=phase,
            repository=value["repository"],
            base_ref=value["base_ref"],
            base_sha=value["base_sha"],
            branch=value["branch"],
            delivery_fingerprint=value["delivery_fingerprint"],
            commit_sha=value.get("commit_sha"),
            remote_sha=value.get("remote_sha"),
            pr_number=value.get("pr_number"),
            pr_url=value.get("pr_url"),
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": DELIVERY_JOURNAL_SCHEMA,
            "phase": self.phase.value,
            "repository": self.repository,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "branch": self.branch,
            "delivery_fingerprint": self.delivery_fingerprint,
        }
        for key in ("commit_sha", "remote_sha", "pr_number", "pr_url"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True, slots=True)
class DeliveryJournal:
    """Atomic mode-0600 journal inside an explicit mode-0700 state directory."""

    state_dir: Path
    delivery_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.state_dir, Path) or not self.state_dir.is_absolute():
            raise DeliveryJournalError("journal_directory")
        _require_sha256(self.delivery_fingerprint, "delivery_fingerprint")
        self._verify_state_dir()

    @property
    def path(self) -> Path:
        return self.state_dir / f"{self.delivery_fingerprint}.json"

    @property
    def _lock_path(self) -> Path:
        return self.state_dir / f".{self.delivery_fingerprint}.lock"

    def _verify_state_dir(self) -> None:
        try:
            info = os.lstat(self.state_dir)
        except OSError:
            raise DeliveryJournalError("journal_directory") from None
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
        ):
            raise DeliveryJournalError("journal_directory")

    def _load_unlocked(self) -> DeliveryJournalRecord | None:
        self._verify_state_dir()
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            return None
        except OSError:
            raise DeliveryJournalError("journal_load") from None
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or not 0 < info.st_size <= _MAX_JOURNAL_BYTES
        ):
            raise DeliveryJournalError("journal_load")
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(self.path, flags)
            opened_file = os.fdopen(descriptor, "rb")
            descriptor = None
            with opened_file as journal_file:
                opened_info = os.fstat(journal_file.fileno())
                if (
                    not stat.S_ISREG(opened_info.st_mode)
                    or stat.S_IMODE(opened_info.st_mode) != 0o600
                    or opened_info.st_uid != os.getuid()
                    or not 0 < opened_info.st_size <= _MAX_JOURNAL_BYTES
                ):
                    raise DeliveryJournalError("journal_load")
                raw = journal_file.read(_MAX_JOURNAL_BYTES + 1)
            if len(raw) > _MAX_JOURNAL_BYTES:
                raise DeliveryJournalError("journal_load")
            decoded = json.loads(raw.decode("utf-8"))
            record = DeliveryJournalRecord.from_dict(decoded)
        except DeliveryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise DeliveryJournalError("journal_load") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if record.delivery_fingerprint != self.delivery_fingerprint:
            raise DeliveryJournalError("journal_identity")
        return record

    def load(self) -> DeliveryJournalRecord | None:
        """Load one fully validated atomic snapshot, or ``None`` before creation."""

        return self._load_unlocked()

    @staticmethod
    def _validate_transition(
        previous: DeliveryJournalRecord | None,
        current: DeliveryJournalRecord,
    ) -> None:
        if previous is None:
            if current.phase is not DeliveryPhase.prepared:
                raise DeliveryJournalError("journal_transition")
            return
        identity_fields = (
            "repository",
            "base_ref",
            "base_sha",
            "branch",
            "delivery_fingerprint",
        )
        if any(
            getattr(previous, field_name) != getattr(current, field_name)
            for field_name in identity_fields
        ):
            raise DeliveryJournalError("journal_identity")
        if _PHASE_RANK[current.phase] < _PHASE_RANK[previous.phase]:
            raise DeliveryJournalError("journal_transition")
        for field_name in ("commit_sha", "pr_number", "pr_url"):
            old_value = getattr(previous, field_name)
            new_value = getattr(current, field_name)
            if old_value is not None and new_value != old_value:
                raise DeliveryJournalError("journal_identity")
        if (
            previous.remote_sha is not None
            and current.remote_sha != previous.remote_sha
            and not (
                _PHASE_RANK[current.phase] > _PHASE_RANK[previous.phase]
                and current.phase
                in {DeliveryPhase.pr_observed, DeliveryPhase.pr_created}
                and current.pr_number is not None
            )
        ):
            raise DeliveryJournalError("journal_identity")

    def _open_lock(self) -> int:
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(self._lock_path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
            ):
                raise OSError("unsafe journal lock")
            return descriptor
        except OSError:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise DeliveryJournalError("journal_lock") from None

    def record(self, record: DeliveryJournalRecord) -> None:
        """Atomically persist a monotonic source-free recovery snapshot."""

        if not isinstance(record, DeliveryJournalRecord):
            raise DeliveryJournalError("journal_record")
        if record.delivery_fingerprint != self.delivery_fingerprint:
            raise DeliveryJournalError("journal_identity")
        self._verify_state_dir()
        lock_descriptor = self._open_lock()
        temp_path: str | None = None
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            previous = self._load_unlocked()
            self._validate_transition(previous, record)
            encoded = _canonical_json(record.to_dict())
            if not encoded or len(encoded) > _MAX_JOURNAL_BYTES:
                raise DeliveryJournalError("journal_record")
            descriptor: int | None
            descriptor, temp_path = tempfile.mkstemp(
                prefix=f".{self.delivery_fingerprint}.",
                suffix=".tmp",
                dir=self.state_dir,
            )
            try:
                os.fchmod(descriptor, 0o600)
                opened_file = os.fdopen(descriptor, "wb")
                descriptor = None
                with opened_file as journal_file:
                    journal_file.write(encoded)
                    journal_file.flush()
                    os.fsync(journal_file.fileno())
                os.replace(temp_path, self.path)
                temp_path = None
                final_info = os.lstat(self.path)
                if (
                    not stat.S_ISREG(final_info.st_mode)
                    or stat.S_IMODE(final_info.st_mode) != 0o600
                    or final_info.st_uid != os.getuid()
                ):
                    raise DeliveryJournalError("journal_record")
                directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_descriptor = os.open(self.state_dir, directory_flags)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except DeliveryError:
                raise
            except OSError:
                raise DeliveryJournalError("journal_record") from None
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        except DeliveryError:
            raise
        except OSError:
            raise DeliveryJournalError("journal_record") from None
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_descriptor)
            except OSError:
                pass
