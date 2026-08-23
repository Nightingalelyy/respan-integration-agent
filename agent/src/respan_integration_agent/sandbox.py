"""Credential-free ephemeral checkout for a session.

v0: a local temp dir (run on your machine).
v1: the same interface, backed by a Railway container that is torn down after the session.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit


CHECKOUT_TIMEOUT_SECONDS = 120.0
_NETWORK_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


class CheckoutError(RuntimeError):
    """A checkout failed without exposing a repository URL or subprocess output."""


def _credential_free_git_env(home: Path) -> dict[str, str]:
    """Build a small Git environment with no ambient credential/config hooks."""

    if not home.is_absolute() or not home.is_dir():
        raise CheckoutError("isolated Git home is invalid")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/usr/bin/false",
    }
    for name in ("LANG", "LC_ALL"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _validate_checkout_target(repo_url: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in repo_url):
        raise CheckoutError("checkout configuration is invalid")
    is_network = _NETWORK_URL_RE.match(repo_url) is not None
    # Git accepts both ``user@host:path`` and ``host:path`` as SSH transports.
    # Treat either spelling as a network URL so a missing scheme cannot bypass the
    # credential-free HTTPS policy.
    looks_like_scp = re.match(r"^(?:[^/@:\\]+@)?[^/:\\]+:", repo_url) is not None
    if looks_like_scp:
        raise CheckoutError("SSH repository URLs are forbidden")
    if not is_network:
        return
    try:
        parsed = urlsplit(repo_url)
        port = parsed.port
    except ValueError:
        raise CheckoutError("network repository URL is invalid") from None
    if (
        not repo_url.startswith("https://")
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or "%" in repo_url
        or "\\" in repo_url
    ):
        raise CheckoutError(
            "only credential-free canonical HTTPS repositories are allowed"
        )


def _validate_base_branch(base_branch: str) -> None:
    if (
        len(base_branch.encode("utf-8")) > 200
        or base_branch.startswith(("-", ".", "/", "refs/"))
        or base_branch.endswith((".", "/", ".lock"))
        or ".." in base_branch
        or "//" in base_branch
        or "@{" in base_branch
        or "\\" in base_branch
        or any(character in base_branch for character in " ~^:?*[]")
        or any(
            not component or component.startswith(".") or component.endswith(".lock")
            for component in base_branch.split("/")
        )
    ):
        raise CheckoutError("checkout base branch is invalid")


@contextmanager
def checkout(repo_url: str, base_branch: str = "main") -> Iterator[Path]:
    """Clone a trusted public/local repository without forwarding credentials."""

    if not isinstance(repo_url, str) or not repo_url or "\x00" in repo_url:
        raise CheckoutError("checkout configuration is invalid")
    if not isinstance(base_branch, str) or not base_branch or "\x00" in base_branch:
        raise CheckoutError("checkout configuration is invalid")
    _validate_checkout_target(repo_url)
    _validate_base_branch(base_branch)
    with tempfile.TemporaryDirectory(prefix="respan-integration-agent-") as tmp:
        root = Path(tmp)
        workdir = root / "repo"
        home = root / "home"
        home.mkdir(mode=0o700)
        try:
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--no-local",
                    "--depth",
                    "1",
                    "--branch",
                    base_branch,
                    "--",
                    repo_url,
                    str(workdir),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=CHECKOUT_TIMEOUT_SECONDS,
                env=_credential_free_git_env(home),
            )
        except (OSError, subprocess.SubprocessError):
            raise CheckoutError("credential-free checkout failed") from None
        yield workdir


def checkout_head(workdir: Path) -> str:
    """Return the exact checked-out commit without reflecting Git output on failure."""

    try:
        with tempfile.TemporaryDirectory(prefix="respan-git-home-") as temp_home:
            home = Path(temp_home)
            home.chmod(0o700)
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=workdir,
                check=True,
                capture_output=True,
                text=True,
                timeout=10.0,
                env=_credential_free_git_env(home),
            )
    except (OSError, subprocess.SubprocessError):
        raise CheckoutError("checkout identity could not be read") from None
    commit = result.stdout.strip()
    if (
        len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
        or int(commit, 16) == 0
    ):
        raise CheckoutError("checkout identity is invalid")
    return commit
