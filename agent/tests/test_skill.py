import os
import shutil

import pytest

from respan_integration_agent.skill import (
    SkillProvisionError,
    bundled_skill_dir,
    provision_respan_skill,
    validate_skill_source,
)


def test_bundled_skill_is_exact_and_isolated():
    validate_skill_source(bundled_skill_dir())
    with provision_respan_skill() as provisioned:
        assert provisioned.config_dir.exists()
        assert provisioned.skill_dir != bundled_skill_dir()
        assert provisioned.tracing_reference.is_file()
        validate_skill_source(provisioned.skill_dir)
        config_dir = provisioned.config_dir
    assert not config_dir.exists()


def test_modified_skill_is_rejected(tmp_path):
    copied = tmp_path / "respan"
    shutil.copytree(bundled_skill_dir(), copied)
    entrypoint = copied / "SKILL.md"
    entrypoint.write_text(entrypoint.read_text() + "\nmodified\n")
    with pytest.raises(SkillProvisionError, match="hash mismatch"):
        validate_skill_source(copied)


@pytest.mark.parametrize("extra_path", ["EXTRA.md", "references/extra.md"])
def test_extra_skill_file_is_rejected(tmp_path, extra_path):
    copied = tmp_path / "respan"
    shutil.copytree(bundled_skill_dir(), copied)
    extra = copied / extra_path
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("unreviewed\n")

    with pytest.raises(SkillProvisionError, match="unexpected paths"):
        validate_skill_source(copied)


def test_extra_skill_directory_is_rejected(tmp_path):
    copied = tmp_path / "respan"
    shutil.copytree(bundled_skill_dir(), copied)
    (copied / "scripts").mkdir()

    with pytest.raises(SkillProvisionError, match="unexpected paths"):
        validate_skill_source(copied)


def test_skill_symlink_is_rejected(tmp_path):
    copied = tmp_path / "respan"
    shutil.copytree(bundled_skill_dir(), copied)
    (copied / "linked.md").symlink_to(copied / "SKILL.md")

    with pytest.raises(SkillProvisionError, match="cannot be a symlink"):
        validate_skill_source(copied)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="mkfifo is unavailable")
def test_non_regular_skill_path_is_rejected(tmp_path):
    copied = tmp_path / "respan"
    shutil.copytree(bundled_skill_dir(), copied)
    os.mkfifo(copied / "unreviewed.fifo")

    with pytest.raises(SkillProvisionError, match="not a regular file"):
        validate_skill_source(copied)
