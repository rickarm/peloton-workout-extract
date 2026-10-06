"""Tests for csv_dir: where the workout CSV lives.

The default must sit outside git repos and outside the macOS folders protected
per app (Downloads, Desktop, Documents), where an unpermitted process hangs
instead of failing.
"""

import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import csv_dir  # noqa: E402


def test_default_is_under_local_share(tmp_path):
    got = csv_dir.resolve_csv_dir(env={}, home=tmp_path)
    assert got == (tmp_path / ".local/share/peloton-sync/csv").resolve()


def test_env_overrides_default(tmp_path):
    target = tmp_path / "elsewhere"
    got = csv_dir.resolve_csv_dir(env={"PELOTON_CSV_DIR": str(target)}, home=tmp_path)
    assert got == target.resolve()


@pytest.mark.parametrize("name", ["Downloads", "Desktop", "Documents"])
def test_protected_folders_are_refused(tmp_path, name):
    for raw in (tmp_path / name, tmp_path / name / "peloton"):
        with pytest.raises(csv_dir.CsvDirError, match=name):
            csv_dir.resolve_csv_dir(env={"PELOTON_CSV_DIR": str(raw)}, home=tmp_path)


def test_symlink_into_protected_folder_is_refused(tmp_path):
    (tmp_path / "Downloads").mkdir()
    link = tmp_path / "sneaky"
    link.symlink_to(tmp_path / "Downloads")
    with pytest.raises(csv_dir.CsvDirError, match="Downloads"):
        csv_dir.resolve_csv_dir(env={"PELOTON_CSV_DIR": str(link)}, home=tmp_path)


def test_dir_inside_git_repo_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(csv_dir.CsvDirError, match="git repository"):
        csv_dir.resolve_csv_dir(
            env={"PELOTON_CSV_DIR": str(repo / "csv")}, home=tmp_path
        )


def test_ensure_creates_with_mode_700(tmp_path):
    target = tmp_path / "a" / "b" / "csv"
    csv_dir.ensure_csv_dir(target)
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_ensure_leaves_existing_dir_alone(tmp_path):
    target = tmp_path / "csv"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    csv_dir.ensure_csv_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
