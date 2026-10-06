"""Where the Peloton workout CSV lives: one setting shared with sync-peloton-airtable.

PELOTON_CSV_DIR (environment) wins; otherwise ~/.local/share/peloton-sync/csv.
The default sits outside every git repo and outside the macOS folders that are
protected per app (Downloads, Desktop, Documents). A process without that
permission doesn't get an error there: it hangs, so those folders are refused.

sync-peloton-airtable's peloton-sync.sh applies the same rules; keep them in step.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping, Optional

ENV_KEY = "PELOTON_CSV_DIR"
DEFAULT_SUBPATH = ".local/share/peloton-sync/csv"
PROTECTED = ("Downloads", "Desktop", "Documents")


class CsvDirError(ValueError):
    pass


def resolve_csv_dir(
    env: Optional[Mapping[str, str]] = None, home: Optional[Path] = None
) -> Path:
    """Return the CSV directory (absolute, not created). Raise CsvDirError if unsafe."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    raw = env.get(ENV_KEY) or str(home / DEFAULT_SUBPATH)
    # Check the protected folders on the lexical path first, without touching
    # the filesystem: even a stat inside them can stall for an unpermitted process.
    path = Path(os.path.abspath(os.path.expanduser(raw)))

    _refuse_protected(path, home)
    path = path.resolve()  # then again after following symlinks
    _refuse_protected(path, home.resolve())
    if _inside_git_repo(path):
        raise CsvDirError(
            f"{ENV_KEY}={path} is inside a git repository. Workout CSVs are personal "
            f"data; pick a directory outside any repo."
        )
    return path


def _refuse_protected(path: Path, home: Path) -> None:
    for name in PROTECTED:
        protected = Path(os.path.abspath(home / name))
        if path == protected or protected in path.parents:
            raise CsvDirError(
                f"{ENV_KEY}={path} is inside ~/{name}, which macOS protects per app "
                f"(a process without permission hangs there). Pick another directory."
            )


def ensure_csv_dir(path: Path) -> Path:
    """Create the directory with mode 700 if missing (an existing one is left as is)."""
    if not path.is_dir():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    return path


def _inside_git_repo(path: Path) -> bool:
    probe = path
    while not probe.exists():
        probe = probe.parent
    try:
        r = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"
