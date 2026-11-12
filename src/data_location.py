"""Where the data lives — kept apart from the code that runs (Nov 2026, M3).

The scan's data (data/*.json) is written by the scan workflow and by the
Streamlit app. Code that runs with secrets must never come from a place
those writers can change, so the two can be separated:

  * ``JT_DATA_BRANCH`` — the git branch that holds the data. The app reads
    and writes data/*.json there (GitHub contents API) and the workflows
    publish there. Unset: the repository's default branch — exactly the
    behaviour before this setting existed. With a separate data branch, the
    default branch can be protected (pull requests only) and the data
    writers' credentials never need to touch code that runs.
  * ``JT_DATA_ROOT`` — a directory holding a checkout of that branch (the
    workflows check it out next to the code). Only its data/*.json files are
    read: nothing in it is ever imported or executed. Unset: this checkout.

Standard library only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # the code checkout (this file is in src/)

# a git branch name, conservatively: no "..", no leading "-" or "/", no
# refs/ prefix, no characters git or a URL would treat specially
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")


class DataLocationError(ValueError):
    """JT_DATA_BRANCH or JT_DATA_ROOT is set to something unusable."""


def valid_branch(name: object) -> bool:
    return (isinstance(name, str) and bool(_BRANCH_RE.fullmatch(name)) and ".." not in name
            and not name.endswith((".lock", "/", ".")) and "//" not in name and "/." not in name
            and not name.startswith("refs/"))


def data_branch(env=None) -> str | None:
    """The data branch, or None for the repository's default branch. A
    malformed value is refused loudly rather than silently ignored: falling
    back to the default branch would write data where code lives."""
    value = (env if env is not None else os.environ).get("JT_DATA_BRANCH", "").strip()
    if not value:
        return None
    if not valid_branch(value):
        raise DataLocationError("JT_DATA_BRANCH is not a valid branch name")
    return value


def data_root(env=None) -> Path:
    """The checkout whose data/ holds the JSON files (default: this one)."""
    value = (env if env is not None else os.environ).get("JT_DATA_ROOT", "").strip()
    if not value:
        return BASE
    root = Path(value).resolve()
    if not (root / "data").is_dir():
        raise DataLocationError("JT_DATA_ROOT has no data/ directory")
    return root
