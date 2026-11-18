"""Hand scan results from the job that reads the web to the job that publishes.

check_jobs.yml (and logos.yml) split each run in two (JT-01, Oct 2026):

  * the job that loads external career sites (a headless browser and HTTP
    clients reading content anyone could control) has a read-only token,
    no stored git credentials and no secrets;
  * the job that holds the secrets and may push only receives that job's
    DATA files, through a workflow artifact, never its code or environment.

``python scan_handoff.py <dir> <file>...`` checks each named file from the
artifact directory and copies it over the checkout's copy: a regular file
(no link), at most MAX_BYTES, valid UTF-8 JSON of the expected shape. Any
problem stops the run before anything is published or emailed. Standard
library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from data_location import data_root
from workday_state import FILES as RECOVERY_FILES, validate as validate_recovery, validate_projection

BASE = Path(__file__).resolve().parent.parent          # the repository root (this file is in src/)
DATA = data_root() / "data"          # where the handed-over files go (the data checkout, JT_DATA_ROOT)
MAX_BYTES = 20 * 1024 * 1024
# the only files that may be handed over, and the JSON shape each must have
SHAPES = {
    "companies.json": "list of objects with an id",
    "seen_jobs.json": "list of objects",
    "logos.json": "object with a logos object",
    "workday_scan_state.json": "versioned M7 safety state",
    "workday_verdict_cache.json": "versioned M7 verdict cache",
}


class HandoffError(ValueError):
    """A handed-over file is missing, oversized, linked or of the wrong shape."""


def _check(name: str, data) -> None:
    if name in RECOVERY_FILES:
        try:
            validate_recovery(name, data)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise HandoffError(f"{name}: invalid recovery schema") from None
        return
    if name == "companies.json":
        ok = isinstance(data, list) and all(isinstance(c, dict) and c.get("id") for c in data)
        if ok:
            for company in data:
                rec = (company.get("scan") or {}).get("recovery")
                if rec is not None:
                    try:
                        validate_projection(rec)
                        if rec["company_id"] != company["id"]:
                            raise ValueError("company mismatch")
                    except (ValueError, KeyError, TypeError):
                        raise HandoffError("invalid monitoring projection") from None
    elif name == "seen_jobs.json":
        ok = isinstance(data, list) and all(isinstance(j, dict) for j in data)
    else:
        ok = isinstance(data, dict) and isinstance(data.get("logos"), dict)
    if not ok:
        raise HandoffError(f"{name}: not a {SHAPES[name]}")


def take_over(src_dir: Path, names: list[str], dest_dir: Path = DATA) -> None:
    """Validate every file first, then copy them all (never half a hand-over)."""
    blobs = {}
    for name in names:
        if name not in SHAPES:
            raise HandoffError(f"{name}: not a file that may be handed over")
        src = Path(src_dir) / name
        if src.is_symlink() or not src.is_file():
            raise HandoffError(f"{name}: missing or not a regular file")
        raw = src.read_bytes()
        if len(raw) > MAX_BYTES:
            raise HandoffError(f"{name}: larger than {MAX_BYTES} bytes")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HandoffError(f"{name}: not valid JSON") from None
        _check(name, data)
        blobs[name] = raw
    if any(name in names for name in RECOVERY_FILES):
        if not all(name in names for name in RECOVERY_FILES):
            raise HandoffError("incomplete recovery generation")
        generations = [json.loads(blobs[name])["run_id"] for name in RECOVERY_FILES]
        if not generations[0] or len(set(generations)) != 1:
            raise HandoffError("mixed recovery generations")
        if "companies.json" in blobs:
            runs = {(c.get("scan") or {}).get("run", {}).get("run_id") for c in json.loads(blobs["companies.json"])
                    if (c.get("scan") or {}).get("run", {}).get("recovery_version") == 1}
            if runs != {generations[0]}:
                raise HandoffError("company/recovery generation mismatch")
            ids = {c["id"] for c in json.loads(blobs["companies.json"])}
            state = json.loads(blobs["workday_scan_state.json"])
            cache = json.loads(blobs["workday_verdict_cache.json"])
            if not set(state["companies"]) <= ids or any(e["company_id"] not in ids for e in cache["entries"].values()):
                raise HandoffError("unknown recovery company")
    for name, raw in blobs.items():
        (Path(dest_dir) / name).write_bytes(raw)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print("usage: scan_handoff.py <artifact dir> <file>...", file=sys.stderr)
        return 2
    try:
        take_over(Path(argv[0]), argv[1:])
    except HandoffError as e:
        print(f"[handoff] refused: {e}", file=sys.stderr)
        return 1
    print(f"[handoff] took over {', '.join(argv[1:])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
