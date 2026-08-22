from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from respan_integration_agent.patch import PatchCaptureError, capture_worktree_patch


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _tree(path: Path) -> dict[str, tuple[bytes, int]]:
    result: dict[str, tuple[bytes, int]] = {}
    for item in path.rglob("*"):
        if ".git" in item.parts or not item.is_file():
            continue
        result[item.relative_to(path).as_posix()] = (
            item.read_bytes(),
            os.stat(item).st_mode & 0o111,
        )
    return result


def test_complete_patch_round_trips_staged_unstaged_untracked_and_binary(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "test")
    _git(source, "config", "user.email", "test@example.com")
    (source / "unstaged.txt").write_text("before\n")
    (source / "staged.txt").write_text("before\n")
    (source / "deleted.txt").write_text("delete me\n")
    (source / "renamed.txt").write_text("rename me\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "baseline")

    replay = tmp_path / "replay"
    _git(tmp_path, "clone", str(source), str(replay))

    (source / "unstaged.txt").write_text("after\n")
    (source / "staged.txt").write_text("staged after\n")
    _git(source, "add", "staged.txt")
    (source / "untracked.txt").write_text("new\n")
    (source / "binary.bin").write_bytes(bytes(range(256)))
    (source / "deleted.txt").unlink()
    _git(source, "mv", "renamed.txt", "renamed new.txt")

    captured = capture_worktree_patch(source)
    assert set(captured.changed_files) == {
        "binary.bin",
        "deleted.txt",
        "renamed new.txt",
        "renamed.txt",
        "staged.txt",
        "unstaged.txt",
        "untracked.txt",
    }
    subprocess.run(
        ["git", "apply", "--binary", "-"],
        cwd=replay,
        input=captured.diff.encode(),
        check=True,
        capture_output=True,
    )
    assert _tree(replay) == _tree(source)


def test_ignored_untracked_file_is_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "test")
    _git(source, "config", "user.email", "test@example.com")
    (source / ".gitignore").write_text(".env\n")
    (source / "app.py").write_text("print('baseline')\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "baseline")
    (source / ".env").write_text("RESPAN_API_KEY=hidden\n")

    with pytest.raises(PatchCaptureError, match="ignored untracked files"):
        capture_worktree_patch(source)
