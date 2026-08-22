"""Capture a complete, replayable patch without mutating the checkout index."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class PatchCaptureError(RuntimeError):
    """Git could not produce a complete review patch."""


@dataclass(frozen=True)
class CapturedPatch:
    changed_files: list[str]
    diff: str


def _git(
    workdir: Path,
    *args: str,
    env: dict[str, str] | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=workdir,
            env=env,
            check=True,
            capture_output=True,
            text=text,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else exc.stderr
        )
        raise PatchCaptureError(
            f"git {' '.join(args)} failed: {(stderr or '').strip()}"
        ) from exc


def capture_worktree_patch(workdir: Path) -> CapturedPatch:
    """Return the final worktree state versus HEAD, including staged/untracked files."""
    workdir = workdir.resolve()
    ignored_result = _git(
        workdir,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        text=False,
    )
    ignored_files = [
        item.decode("utf-8", errors="surrogateescape")
        for item in ignored_result.stdout.split(b"\0")
        if item
    ]
    if ignored_files:
        preview = ", ".join(ignored_files[:20])
        suffix = " ..." if len(ignored_files) > 20 else ""
        raise PatchCaptureError(
            "checkout contains ignored untracked files that cannot be reviewed: "
            f"{preview}{suffix}"
        )

    with tempfile.TemporaryDirectory(prefix="respan-v0-index-") as temp_root:
        index_path = Path(temp_root) / "index"
        git_env = os.environ.copy()
        git_env["GIT_INDEX_FILE"] = str(index_path)

        _git(workdir, "read-tree", "HEAD", env=git_env)
        _git(workdir, "add", "-A", "--", ".", env=git_env)
        _git(workdir, "diff", "--cached", "--check", "HEAD", env=git_env)

        name_result = _git(
            workdir,
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "-z",
            "HEAD",
            env=git_env,
            text=False,
        )
        changed_files = [
            item.decode("utf-8", errors="surrogateescape")
            for item in name_result.stdout.split(b"\0")
            if item
        ]

        diff_result = _git(
            workdir,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-renames",
            "HEAD",
            env=git_env,
        )
        return CapturedPatch(changed_files=changed_files, diff=diff_result.stdout)
