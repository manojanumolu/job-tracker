"""M7 public data schemas and merge rules. No network, credentials or executable data."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = "workday_scan_state.json"
CACHE_FILE = "workday_verdict_cache.json"
FILES = (STATE_FILE, CACHE_FILE)
MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 10000
HOST = re.compile(r"[a-z0-9-]+\.wd[0-9]+\.myworkdayjobs\.com\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")
OUTCOMES = {"healthy", "degraded", "failed", "rate_limited", "deferred", "cooling_down", "not_checked", "needs_config"}
CATEGORIES = {"", "rate_limit", "forbidden", "authentication", "not_found", "http_error", "server_error",
              "timeout", "network", "budget", "cooldown", "circuit", "state_unavailable", "configuration",
              "incomplete", "invalid_response", "redirect", "rotation", "canary_gate", "cluster_gate"}
# Rate-limit response headers worth keeping as evidence (values are bounded text).
HEADER_ALLOW = ("retry-after", "ratelimit", "ratelimit-policy", "ratelimit-limit", "ratelimit-remaining",
                "ratelimit-reset", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
                "x-rate-limit-limit", "x-rate-limit-remaining", "x-rate-limit-reset", "server", "via")


def safe_headers(headers) -> dict:
    """Allow-listed, bounded, printable header values; never cookies or auth."""
    out = {}
    for name in HEADER_ALLOW:
        value = headers.get(name) if headers is not None else None
        if isinstance(value, str) and value:
            value = "".join(ch for ch in value if 32 <= ord(ch) < 127)[:200]
            if value:
                out[name] = value
    return out


def validate_projection(rec: dict) -> None:
    required = {"version", "company_id", "discovery_method", "outcome", "failure_category", "http_status", "host",
                "retry_after_s", "retry_after_valid", "cooldown_until", "last_attempt", "last_success", "observed_at",
                "source", "unresolved_failure", "pending", "oldest_pending", "overdue", "requests"}
    if not isinstance(rec, dict) or not required <= set(rec) or set(rec) - required - {"cache_hits", "cache_misses", "uncacheable", "pending_overflow"}:
        raise ValueError("invalid monitoring projection")
    if (type(rec["version"]) is not int or rec["version"] != 1 or not _text(rec["company_id"], 100)
            or rec["outcome"] not in OUTCOMES or rec["failure_category"] not in CATEGORIES
            or not isinstance(rec["source"], str) or not HEX.fullmatch(rec["source"])):
        raise ValueError("invalid monitoring projection identity")
    if not _text(rec["discovery_method"], 100) or any(k not in {"workday", "workday_sitemap"} for k in rec["discovery_method"].split(", ")):
        raise ValueError("invalid monitoring method")
    if not isinstance(rec["host"], str) or any(not HOST.fullmatch(h) for h in rec["host"].split(", ")):
        raise ValueError("invalid monitoring host")
    if rec["http_status"] is not None and (type(rec["http_status"]) is not int or not 100 <= rec["http_status"] <= 599):
        raise ValueError("invalid monitoring HTTP status")
    if any(type(rec[k]) is not bool for k in ("retry_after_valid", "unresolved_failure", "overdue")):
        raise ValueError("invalid monitoring flag")
    if any(not _time(rec[k]) for k in ("cooldown_until", "last_attempt", "last_success", "observed_at", "oldest_pending")):
        raise ValueError("invalid monitoring timestamp")
    if type(rec["retry_after_s"]) not in (int, float) or not 0 <= rec["retry_after_s"] <= 253402214400:
        raise ValueError("invalid monitoring retry delay")
    for key in ("pending", "requests", "cache_hits", "cache_misses", "uncacheable", "pending_overflow"):
        if key in rec and (type(rec[key]) is not int or not 0 <= rec[key] <= 1000000):
            raise ValueError("invalid monitoring count")


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch(value) -> float:
    if not isinstance(value, str):
        return 0
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d.timestamp() if d.tzinfo else 0
    except (ValueError, OverflowError, OSError):
        return 0


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def empty(kind: str) -> dict:
    return {"schema_version": 1, "run_id": "", "updated_at": "", "entries" if kind == CACHE_FILE else "hosts": {},
            **({"companies": {}, "last_completed_run": {}} if kind == STATE_FILE else {})}


def _text(v, limit=256):
    return isinstance(v, str) and len(v) <= limit and not any(ord(c) < 32 and c not in "\n\t" for c in v)


def _time(v):
    return isinstance(v, str) and (v == "" or (0 < epoch(v) < 253402214400))


def validate(name: str, data: object) -> None:
    """Closed schemas: artifacts cannot smuggle credentials, headers or arbitrary fields."""
    def require(ok):
        if not ok:
            raise ValueError(f"invalid {name} schema")
    require(name in FILES and isinstance(data, dict))
    base = {"schema_version", "run_id", "updated_at"}
    require(set(data) == base | ({"entries"} if name == CACHE_FILE else {"hosts", "companies", "last_completed_run"}))
    require(type(data["schema_version"]) is int and data["schema_version"] == 1)
    require(_text(data["run_id"], 100) and _time(data["updated_at"]))
    require(len(json.dumps(data).encode()) <= MAX_BYTES)
    if name == CACHE_FILE:
        entries = data["entries"]
        require(isinstance(entries, dict) and len(entries) <= MAX_ENTRIES)
        for key, entry in entries.items():
            require(bool(HEX.fullmatch(key)) and isinstance(entry, dict))
            require(set(entry) == {"company_id", "source", "fingerprint", "rules", "evaluated_at", "expires_at", "title", "detail", "verdict"})
            require(_text(entry["company_id"], 100) and all(isinstance(entry[k], str) and HEX.fullmatch(entry[k]) for k in ("source", "fingerprint", "rules")))
            require(_time(entry["evaluated_at"]) and _time(entry["expires_at"]) and epoch(entry["expires_at"]) > epoch(entry["evaluated_at"]) > 0)
            require(entry["verdict"] in ("accepted", "rejected") and _text(entry["title"], 1000))
            detail = entry["detail"]
            require(isinstance(detail, dict) and set(detail) == {"ok", "description", "location", "strict_text", "posting_evidence", "reason", "matched", "posted_at"})
            require(detail["ok"] is True and type(detail["posting_evidence"]) is bool)
            require(all(_text(detail[k], 100000 if k in ("description", "strict_text") else 2000) for k in detail if k not in ("ok", "posting_evidence")))
        return
    require(isinstance(data["hosts"], dict) and len(data["hosts"]) <= 1000)
    for host, obs in data["hosts"].items():
        require(bool(HOST.fullmatch(host)) and isinstance(obs, dict))
        base = {"observed_at", "cooldown_until", "failures", "http_status", "failure_category", "retry_after_s", "retry_after_valid"}
        require(set(obs) in (base, base | {"headers"}))
        if "headers" in obs:
            require(isinstance(obs["headers"], dict) and len(obs["headers"]) <= len(HEADER_ALLOW))
            require(all(k in HEADER_ALLOW and _text(v, 200) for k, v in obs["headers"].items()))
        require(_time(obs["observed_at"]) and _time(obs["cooldown_until"]))
        require(type(obs["failures"]) is int and 0 <= obs["failures"] <= 10000)
        require(obs["http_status"] is None or type(obs["http_status"]) is int and 100 <= obs["http_status"] <= 599)
        require(obs["failure_category"] in CATEGORIES and type(obs["retry_after_valid"]) is bool)
        require(type(obs["retry_after_s"]) in (int, float) and 0 <= obs["retry_after_s"] <= 253402214400)
    require(isinstance(data["companies"], dict) and len(data["companies"]) <= 1000)
    for cid, obs in data["companies"].items():
        require(_text(cid, 100) and isinstance(obs, dict))
        require(set(obs) == {"source", "observed_at", "last_attempt", "last_success", "outcome", "pending"})
        require(isinstance(obs["source"], str) and bool(HEX.fullmatch(obs["source"])))
        require(all(_time(obs[k]) for k in ("observed_at", "last_attempt", "last_success")) and obs["outcome"] in OUTCOMES)
        require(isinstance(obs["pending"], dict) and len(obs["pending"]) <= MAX_ENTRIES)
        for key, pending in obs["pending"].items():
            require(bool(HEX.fullmatch(key)) and isinstance(pending, dict) and set(pending) == {"since", "reason"})
            require(_time(pending["since"]) and pending["reason"] in CATEGORIES)
    run = data["last_completed_run"]
    require(isinstance(run, dict))
    if run:
        require(set(run) == {"run_id", "started_at", "finished_at"})
        require(_text(run["run_id"], 100) and _time(run["started_at"]) and _time(run["finished_at"]))
        require(epoch(run["finished_at"]) >= epoch(run["started_at"]) > 0)


def read(path: Path, name: str) -> dict:
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise ValueError("unsafe M7 state file")
    data = json.loads(path.read_text("utf-8"))
    try:
        validate(name, data)
    except (KeyError, TypeError, AttributeError, OverflowError) as e:
        raise ValueError("invalid M7 schema") from e
    return data


def write(path: Path, name: str, data: dict) -> None:
    validate(name, data)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=True, separators=(",", ":")) + "\n", encoding="utf-8")
    temp.replace(path)


def merge(name: str, remote: dict | None, local: dict) -> dict:
    validate(name, local)
    if remote is None:
        return copy.deepcopy(local)
    validate(name, remote)  # Never overwrite an unsupported/corrupt remote safety record.
    newer = epoch(local["updated_at"]) > epoch(remote["updated_at"])
    out = copy.deepcopy(local if newer else remote)
    fields = ("entries",) if name == CACHE_FILE else ("hosts", "companies")
    for field in fields:
        out[field] = copy.deepcopy(remote[field])
        for key, value in local[field].items():
            old = out[field].get(key)
            clock = "evaluated_at" if field == "entries" else "observed_at"
            if old is None or epoch(value[clock]) > epoch(old[clock]):
                out[field][key] = copy.deepcopy(value)
            elif field == "hosts" and epoch(value[clock]) == epoch(old[clock]) and epoch(value["cooldown_until"]) > epoch(old["cooldown_until"]):
                out[field][key] = copy.deepcopy(value)
    if name == CACHE_FILE:
        # Prune against the merged generation, never a stale writer's entry list.
        at = max(epoch(local["updated_at"]), epoch(remote["updated_at"]))
        out["entries"] = {k: v for k, v in out["entries"].items() if epoch(v["expires_at"]) > at}
        out["entries"] = dict(sorted(out["entries"].items(), key=lambda p: p[1]["evaluated_at"], reverse=True)[:MAX_ENTRIES])
        while len(json.dumps(out).encode()) > MAX_BYTES:
            out["entries"].pop(next(reversed(out["entries"])))
    else:
        runs = [r for r in (remote["last_completed_run"], local["last_completed_run"]) if r]
        out["last_completed_run"] = max(runs, key=lambda r: epoch(r["finished_at"])) if runs else {}
    validate(name, out)
    return out


def publication_payload(data_dir: Path) -> dict:
    """Legacy alert-only/test callers need neither file; an enabled producer needs both."""
    present = [(data_dir / name).exists() for name in FILES]
    if not any(present):
        return {}
    if not all(present):
        raise ValueError("incomplete M7 publication")
    values = {name: read(data_dir / name, name) for name in FILES}
    if len({v["run_id"] for v in values.values()}) != 1:
        raise ValueError("mixed M7 artifact generations")
    return {f"data/{name}": value for name, value in values.items()}
