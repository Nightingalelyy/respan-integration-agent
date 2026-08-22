"""Pinned, isolated provisioning for the Respan Claude skill."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class SkillProvisionError(RuntimeError):
    """The pinned Respan skill cannot be safely provisioned."""


PINNED_SKILL_FILES = {
    "SKILL.md": "362573c74407e81c9643443518ff00e09457302c279c9325c4461b5e8d1b1184",
    "references/evals.md": "891f67018af2e22a4d89d4779f4282a7c70f89c4670cb0ec4d79e5dd9dc43271",
    "references/gateway.md": "6bff261df498fc6e6f576b4191c7d5c3fc3d1ca002ff16e2f3838bbed504961a",
    "references/monitors.md": "a2aa9eba2b5c98ba6cec1b8d5d08f2c0192dbeebf6b1589bce1de9975697337e",
    "references/prompts.md": "073420d2d7aeff29b75f3508f99b4b8d191217a49b1d396649ba13e6b3ff4fbc",
    "references/tracing.md": "5a6c8ddbfbe04a6b46b3cbe50a7423683dc6dc670208b70eae87eb2cba13af38",
}


@dataclass(frozen=True)
class ProvisionedSkill:
    config_dir: Path
    skill_dir: Path
    tracing_reference: Path
    gateway_reference: Path


def bundled_skill_dir() -> Path:
    return Path(__file__).resolve().parent / "resources" / "respan"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_skill_source(source: Path) -> None:
    """Require the exact reviewed skill snapshot, including every reference."""
    if source.is_symlink():
        raise SkillProvisionError(
            f"Respan skill directory cannot be a symlink: {source}"
        )
    try:
        source_mode = source.lstat().st_mode
    except FileNotFoundError:
        raise SkillProvisionError(f"Respan skill directory does not exist: {source}")
    if not stat.S_ISDIR(source_mode):
        raise SkillProvisionError(f"Respan skill source is not a directory: {source}")

    source = source.resolve()
    expected_files = set(PINNED_SKILL_FILES)
    expected_directories = {
        parent.as_posix()
        for relative in expected_files
        for parent in Path(relative).parents
        if parent != Path(".")
    }
    actual_files: set[str] = set()
    actual_directories: set[str] = set()

    for current_root, directory_names, file_names in os.walk(source, followlinks=False):
        current = Path(current_root)
        for name in directory_names:
            path = current / name
            relative = path.relative_to(source).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise SkillProvisionError(
                    f"Respan skill path cannot be a symlink: {relative}"
                )
            if not stat.S_ISDIR(mode):
                raise SkillProvisionError(
                    f"Respan skill path is not a regular directory: {relative}"
                )
            actual_directories.add(relative)
        for name in file_names:
            path = current / name
            relative = path.relative_to(source).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise SkillProvisionError(
                    f"Respan skill path cannot be a symlink: {relative}"
                )
            if not stat.S_ISREG(mode):
                raise SkillProvisionError(
                    f"Respan skill path is not a regular file: {relative}"
                )
            actual_files.add(relative)

    unexpected_paths = sorted(
        (actual_files - expected_files) | (actual_directories - expected_directories)
    )
    missing_paths = sorted(
        (expected_files - actual_files) | (expected_directories - actual_directories)
    )
    if unexpected_paths or missing_paths:
        details: list[str] = []
        if unexpected_paths:
            details.append(f"unexpected paths: {', '.join(unexpected_paths)}")
        if missing_paths:
            details.append(f"missing paths: {', '.join(missing_paths)}")
        raise SkillProvisionError(
            f"Respan skill manifest does not match the pinned snapshot ({'; '.join(details)})"
        )

    for relative, expected_hash in PINNED_SKILL_FILES.items():
        path = source / relative
        actual_hash = _sha256(path)
        if actual_hash != expected_hash:
            raise SkillProvisionError(
                f"Respan skill hash mismatch for {relative}: "
                f"expected {expected_hash}, got {actual_hash}"
            )

    entrypoint = (source / "SKILL.md").read_text(encoding="utf-8")
    if "\nname: respan\n" not in entrypoint:
        raise SkillProvisionError("Respan SKILL.md does not declare name: respan")


@contextmanager
def provision_respan_skill(source: Path | None = None) -> Iterator[ProvisionedSkill]:
    """Copy only the reviewed skill into a fresh Claude configuration root."""
    override = os.environ.get("RESPAN_SKILL_DIR")
    selected = Path(override) if override else (source or bundled_skill_dir())
    validate_skill_source(selected)

    with tempfile.TemporaryDirectory(prefix="respan-v0-claude-") as temp_root:
        config_dir = Path(temp_root) / "config"
        skill_dir = config_dir / "skills" / "respan"
        for relative in PINNED_SKILL_FILES:
            destination = skill_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(selected / relative, destination)
        validate_skill_source(skill_dir)
        yield ProvisionedSkill(
            config_dir=config_dir,
            skill_dir=skill_dir,
            tracing_reference=skill_dir / "references" / "tracing.md",
            gateway_reference=skill_dir / "references" / "gateway.md",
        )
