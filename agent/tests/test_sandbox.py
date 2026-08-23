from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from respan_integration_agent.sandbox import (
    CheckoutError,
    _credential_free_git_env,
    checkout,
    checkout_head,
)


def _run_git(workdir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(path: Path) -> str:
    path.mkdir()
    _run_git(path, "init", "-b", "main")
    _run_git(path, "config", "user.name", "sandbox-test")
    _run_git(path, "config", "user.email", "sandbox@example.invalid")
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    _run_git(path, "add", "README.md")
    _run_git(path, "commit", "-m", "fixture")
    return _run_git(path, "rev-parse", "HEAD")


def test_checkout_is_credential_free_and_binds_head(monkeypatch, tmp_path):
    expected = _repository(tmp_path / "source")
    monkeypatch.setenv("GITHUB_TOKEN", "github-sentinel-secret")
    monkeypatch.setenv("RESPAN_GITHUB_TOKEN", "respan-github-sentinel-secret")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "hostile-helper")

    with checkout(str(tmp_path / "source"), "main") as workdir:
        assert checkout_head(workdir) == expected
        config = _run_git(workdir, "config", "--local", "--list")
        assert "sentinel-secret" not in config

    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir(mode=0o700)
    env = _credential_free_git_env(isolated_home)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "/usr/bin/false"
    assert "GITHUB_TOKEN" not in env
    assert "RESPAN_GITHUB_TOKEN" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert env["HOME"] == str(isolated_home)
    assert env["XDG_CONFIG_HOME"] == str(isolated_home)


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://user:github-sentinel-secret@github.com/acme/example.git",
        "https://github-sentinel-secret@github.com/acme/example.git",
        "HTTPS://github.com/acme/example.git",
        "http://github.com/acme/example.git",
        "ssh://git@github.com/acme/example.git",
        "git@github.com:acme/example.git",
        "github.com:acme/example.git",
        "https://github.com:443/acme/example.git",
        "https://github.com/acme/example.git?token=github-sentinel-secret",
        "https://github.com/acme/example.git#github-sentinel-secret",
    ],
)
def test_checkout_rejects_credential_bearing_urls_without_running_git(
    monkeypatch, repo_url
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("credential reached subprocess"),
    )

    with pytest.raises(CheckoutError) as raised:
        with checkout(repo_url):
            pass

    assert "github-sentinel-secret" not in str(raised.value)


def test_checkout_error_does_not_reflect_subprocess_output(monkeypatch, tmp_path):
    hostile = "github-sentinel-secret Authorization: Bearer secret"

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(128, ["git"], stderr=hostile)

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(CheckoutError) as raised:
        with checkout(str(tmp_path / "missing")):
            pass

    assert hostile not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("stdout", ["0" * 40 + "\n", "A" * 40 + "\n", "short\n"])
def test_checkout_head_rejects_invalid_commit_identity(monkeypatch, tmp_path, stdout):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git", "rev-parse"], 0, stdout, ""
        ),
    )

    with pytest.raises(CheckoutError, match="identity is invalid"):
        checkout_head(tmp_path)


@pytest.mark.parametrize(
    "base_branch",
    ["--upload-pack=hostile", "refs/heads/main", "../main", "main.lock", "bad branch"],
)
def test_checkout_rejects_unsafe_base_branch_before_git(monkeypatch, base_branch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("unsafe branch reached subprocess"),
    )

    with pytest.raises(CheckoutError):
        with checkout("https://github.com/acme/example.git", base_branch):
            pass
