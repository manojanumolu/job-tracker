"""M7 scan-scoped cache, rotation and diagnostics. Public state only."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from workday_http import Controller, CURRENT
from workday_state import CACHE_FILE, STATE_FILE, MAX_BYTES, digest, empty, epoch, iso, read, validate, write

BASE = Path(__file__).resolve().parent.parent
log = logging.getLogger("scraper")


def configuration():
    # Trusted code checkout only. Never read a configuration path from scraped data.
    config = json.loads((BASE / "config/workday_recovery.json").read_text("utf-8"))
    mode = os.environ.get("JT_WORKDAY_MODE", config["mode"])
    if mode not in {"auto", "canary", "full", "paused"}:
        raise ValueError("invalid JT_WORKDAY_MODE")
    config["mode"] = mode
    bounds = {"batch_size": (1, 100), "global_spacing_s": (1, 300), "host_spacing_s": (1, 300),
              "company_delay_s": (1, 600), "max_retries": (0, 2), "backoff_s": (1, 120),
              "retry_wait_budget_s": (0, 120), "cooldown_hours": (1, 24), "max_cooldown_hours": (1, 168),
              "api_breaker_hosts": (1, 100), "workday_budget_s": (1, 3600), "run_budget_s": (1, 4800),
              "max_requests": (1, 2000), "cache_ttl_hours": (1, 168), "cache_max_entries": (1, 10000), "overdue_hours": (1, 48),
              "revisit_hours": (1, 48)}
    if set(config) != set(bounds) | {"mode", "canary_company_ids", "cache_enabled"}:
        raise ValueError("unknown Workday configuration")
    for key, (low, high) in bounds.items():
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"invalid Workday setting: {key}")
    if (type(config["cache_enabled"]) is not bool or not isinstance(config["canary_company_ids"], list)
            or not config["canary_company_ids"] or any(not isinstance(cid, str) or not cid or len(cid) > 100 for cid in config["canary_company_ids"])):
        raise ValueError("invalid Workday canary/cache setting")
    return config


def rules_version():
    return digest(["m7-v1", *[(BASE / "src" / f).read_text("utf-8") for f in
                  ("job_classifier.py", "job_filters.py", "locations.py", "scraper.py", "sources.py",
                   "job_requirements.py", "skill_taxonomy.py")]])


def is_api(configs):
    return any(c.get("type") == "workday" for c in configs)


def is_workday(configs):
    return any(c.get("type") in ("workday", "workday_sitemap") for c in configs)


def cluster(host):
    """The Workday data centre of a host: "wd5" for tenant.wd5.myworkdayjobs.com."""
    part = (host or "").split(".")
    return part[1] if len(part) > 2 and part[1].startswith("wd") and part[1][2:].isdigit() else ""


def hosts(configs):
    return sorted({urlsplit(c.get("api") or c.get("site") or "").hostname or "" for c in configs
                   if c.get("type") in ("workday", "workday_sitemap")})


class Recovery:
    def __init__(self, data_dir, companies, sources_for, *, config=None, clock=time.monotonic, wall=time.time, sleep=time.sleep):
        self.data_dir = Path(data_dir)
        self.config = config or configuration()
        self.wall, self.sleep = wall, sleep
        self.run_id = uuid.uuid4().hex
        self.sources_for = sources_for
        self.rules = rules_version()
        self.state_error = ""
        self.cache_error = ""
        self.state = empty(STATE_FILE)
        state_path = self.data_dir / STATE_FILE
        try:
            self.state = read(state_path, STATE_FILE)
        except FileNotFoundError:
            # A prior M7 projection proves this is unexpected data loss, not first deployment.
            if any((c.get("scan") or {}).get("recovery") for c in companies):
                self.state_error = "state_unavailable"
        except (ValueError, OSError, TypeError):
            self.state_error = "state_unavailable"
        self.cache = empty(CACHE_FILE)
        try:
            self.cache = read(self.data_dir / CACHE_FILE, CACHE_FILE)
        except FileNotFoundError:
            pass
        except (ValueError, OSError, TypeError):
            self.cache_error = "cache_rebuilt"
        signatures = {c["id"]: digest(sources_for(c)) for c in companies}
        self.state["companies"] = {cid: value for cid, value in self.state["companies"].items()
                                   if signatures.get(cid) == value["source"]}
        self.cache["entries"] = {key: value for key, value in self.cache["entries"].items()
                                 if signatures.get(value["company_id"]) == value["source"]}
        if not state_path.exists() and not self.state_error:
            # Known 429s are not forgotten on first deployment: one conservative pause,
            # counted from when the 429 was actually recorded (never from the future).
            for c in companies:
                if "HTTP 429" in c.get("status_reason", ""):
                    seen = min(epoch(c.get("last_checked")) or wall(), wall())
                    for host in hosts(sources_for(c)):
                        self.state["hosts"][host] = {"observed_at": iso(seen), "cooldown_until": iso(seen + self.config["cooldown_hours"] * 3600),
                            "failures": 1, "http_status": 429, "failure_category": "rate_limit", "retry_after_s": 0, "retry_after_valid": False}
        self.http = Controller(self.state, self.config, clock=clock, wall=wall, sleep=sleep)
        self.company = None
        self.source = ""
        self.pending = {}
        self.stats = {}
        self.event_start = 0
        self.last_stop = {}
        self.token = None
        self.used_workday = False
        self.mode = self.effective_mode(companies)
        self.not_due = set()
        self.selected = self.select(companies)

    def effective_mode(self, companies):
        """Recovery gating, decided per Workday data centre (the ``wdN`` in the host).

        "auto" (default): each data centre is gated independently. A data centre is
        gated while the newest HTTP 429 on any of its API hosts is newer than the
        newest REAL healthy scan of any company in it. A gated data centre gets
        exactly one probe (configured canaries preferred) once the latest cooldown
        among its throttled hosts has ended; one healthy probe releases the whole
        data centre, even within the same run. A throttle in wd3 no longer holds
        back wd1, wd5 or wd12 (history: 15 Nov wd1-only and 16 Nov wd12-only 429s).
        "canary": legacy global gate (only configured canaries may probe).
        Returns "full" when nothing is gated, else "canary"/"partial" for summaries.
        """
        mode = self.config["mode"]
        self.api_companies = [c for c in companies if is_api(self.sources_for(c))]
        if mode != "auto":
            return mode
        return "partial" if self.gated_clusters() else "full"

    def cluster_of(self, company):
        names = sorted({cluster(h) for h in hosts(self.sources_for(company))} - {""})
        return names[0] if names else ""

    def cluster_status(self, name):
        """(gated, throttled_at, cooldown_until) for one data centre, from durable state."""
        members = [c for c in getattr(self, "api_companies", []) if self.cluster_of(c) == name]
        api_hosts = {h for c in members for h in hosts(self.sources_for(c))}
        limited = [o for h, o in self.state["hosts"].items() if h in api_hosts and o.get("failure_category") == "rate_limit"]
        if not limited:
            return False, 0, 0
        throttled_at = max(epoch(o.get("observed_at")) for o in limited)
        until = max(epoch(o.get("cooldown_until")) for o in limited)
        # last_attempt moves only when requests were really sent (never on a projection)
        healthy = [epoch(self.state["companies"].get(c["id"], {}).get("last_attempt")) for c in members
                   if self.state["companies"].get(c["id"], {}).get("outcome") in ("healthy", "degraded")]
        return not (healthy and max(healthy) > throttled_at), throttled_at, until

    def gated_clusters(self):
        names = {self.cluster_of(c) for c in getattr(self, "api_companies", [])} - {""}
        return {n for n in names if self.cluster_status(n)[0]}

    def _policy_blocked(self, company):
        return any(self.state["hosts"].get(h, {}).get("failure_category") in ("forbidden", "authentication", "redirect")
                   for h in hosts(self.sources_for(company)))

    def probes(self, companies):
        api = [c for c in companies if is_api(self.sources_for(c)) and not self._policy_blocked(c)]
        canaries = self.config["canary_company_ids"]
        if self.mode == "canary":   # legacy global gate
            preferred = [c["id"] for c in api if c["id"] in canaries]
            if preferred:
                return set(preferred)
            oldest = sorted((self.attempt(c), c["id"]) for c in api)
            return {oldest[0][1]} if oldest else set()
        self.probe_clusters = {}
        for name in sorted(self.gated_clusters()):
            self._probe_for(name)
        return set(self.probe_clusters.values())

    def _probe_for(self, name):
        """Pick (once per run) the single probe for a gated data centre, or None while
        its newest throttle still cools down. Configured canaries are preferred."""
        if name in self.probe_clusters:
            return self.probe_clusters[name]
        _, _, until = self.cluster_status(name)
        if until > self.wall():
            return None
        canaries = self.config["canary_company_ids"]
        members = [c for c in getattr(self, "api_companies", []) if self.cluster_of(c) == name
                   and not self._policy_blocked(c)
                   and not any(self.http.cooling(h) for h in hosts(self.sources_for(c)))]
        if not members:
            return None
        rank = {cid: i for i, cid in enumerate(canaries)}     # configured order: sanofi before pwc
        members.sort(key=lambda c: (rank.get(c["id"], len(rank)), self.attempt(c), c["id"]))
        self.probe_clusters[name] = members[0]["id"]
        return members[0]["id"]

    def gate_reason(self, company):
        """"" when the company may be contacted as far as recovery gating goes."""
        if self.mode == "canary":
            return "" if company["id"] in getattr(self, "probe_ids", ()) else "canary_gate"
        name = self.cluster_of(company)
        # A 429 anywhere in a data centre during THIS run closes that data centre for
        # the rest of the run (15 Nov: one wd1 429 was followed by four more).
        if name and any(cluster(h) == name for h in self.http.throttled):
            return "cluster_gate"
        if self.config["mode"] != "auto" or not name or not self.cluster_status(name)[0]:
            return ""          # data centre not gated (never throttled, or released by a healthy scan)
        if company["id"] in getattr(self, "probe_ids", ()):
            return ""
        # A data centre whose cooldown ended after this run started gets its one probe now.
        if not hasattr(self, "probe_clusters"):
            self.probe_clusters = {}
        if name not in self.probe_clusters and self._probe_for(name) == company["id"]:
            self.probe_ids = set(getattr(self, "probe_ids", ())) | {company["id"]}
            return ""
        return "cluster_gate"

    def cluster_summary(self):
        """e.g. "wd1 released, wd3 gated (probe none, cooling), wd5 gated (probe probed)" - ids are not printed."""
        names = sorted({self.cluster_of(c) for c in getattr(self, "api_companies", [])} - {""})
        parts = []
        for n in names:
            gated, _, until = self.cluster_status(n)
            if not gated:
                parts.append(f"{n} open")
            else:
                probe = getattr(self, "probe_clusters", {}).get(n)
                parts.append(f"{n} gated ({'probe sent' if probe else 'cooling' if until > self.wall() else 'no probe'})")
        return ", ".join(parts)

    def order_key(self, company):
        """Probes first, so one healthy probe can release its data centre in the same run."""
        return 0 if company["id"] in getattr(self, "probe_ids", ()) else 1

    def select(self, companies):
        self.probe_ids = self.probes(companies) if self.mode in ("canary", "partial") else set()
        eligible = []
        for c in companies:
            cfg = self.sources_for(c)
            if not is_api(cfg) or self.defer_reason(c, selection=False):
                continue
            attempt = self.attempt(c)
            if not self.due(c):
                self.not_due.add(c["id"])   # checked recently enough: no request this window
                continue
            # Probes first, then oldest attempts: a permanently failing portal cannot monopolize slots.
            eligible.append((self.order_key(c), attempt, c["id"]))
        return {cid for _, _, cid in sorted(eligible)[:self.config["batch_size"]]}

    def attempt(self, company):
        prev = self.state["companies"].get(company["id"], {})
        return epoch(prev.get("last_attempt") or company.get("last_checked"))

    def due(self, company):
        """The revisit interval spaces out SUCCESSFUL scans. After a failure, throttle
        or deferral, retry timing belongs to the host cooldown and its escalation
        (3/6/12/24 h): an old failed attempt must not also push the next try back by
        the revisit interval (seen 19 Nov: canary held 5 h after a 21:04 429)."""
        prev = self.state["companies"].get(company["id"])
        if not prev or prev.get("outcome") not in ("healthy", "degraded"):
            return True
        revisit = (self.config["revisit_hours"] - 1) * 3600   # 1 h slack absorbs late scan starts
        return self.wall() - self.attempt(company) >= revisit

    def defer_reason(self, company, selection=True):
        cfg = self.sources_for(company)
        if not is_workday(cfg):
            return ""
        from workday_alternatives import permitted
        if any(c.get("type") == "workday_sitemap" and not permitted(c) for c in cfg):
            return "configuration"
        if self.state_error:
            return "state_unavailable"
        if any(self.state["hosts"].get(h, {}).get("failure_category") in ("forbidden", "authentication", "redirect") for h in hosts(cfg)):
            return "configuration"
        if is_api(cfg):
            if self.mode == "paused":
                return "configuration"
        if any(self.http.cooling(h) or h in self.http.closed for h in hosts(cfg)):
            return "cooldown"       # a host's own cooldown is reported first ("cooling down until ...")
        if is_api(cfg):
            gate = self.gate_reason(company)
            if gate:
                return gate
        if is_api(cfg) and len(self.http.throttled) >= self.config["api_breaker_hosts"]:
            return "circuit"
        if is_api(cfg) and selection and company["id"] not in self.selected:
            # Selection is a snapshot at run start. A cooldown that has since expired
            # (checked above, strictly) is admitted while the batch still has room,
            # instead of losing a whole 3 h window by seconds (seen 19 Nov 00:03).
            if (company["id"] not in self.not_due and self.due(company)
                    and len(self.selected) < self.config["batch_size"]):
                self.selected.add(company["id"])
                return ""
            return "rotation"
        return ""

    def begin(self, company):
        self.company = company
        self.source = digest(self.sources_for(company))
        previous = self.state["companies"].get(company["id"], {})
        self.pending = dict(previous.get("pending", {})) if previous.get("source") == self.source else {}
        self.stats = {"cache_hits": 0, "cache_misses": 0}
        self.event_start = len(self.http.events)
        self.last_stop = {}
        if self.used_workday:
            self.sleep(self.config["company_delay_s"])
        self.used_workday = True
        self.token = CURRENT.set(self)

    def begin_cache(self, company):
        """The 72 h detail-verdict cache only, for a company read from any other
        official source (Nov 2026): no Workday request gating, cooldown,
        company delay or pending state - just lookup/cache_detail, keyed by
        this company, its source configuration and the posting URL."""
        self.company = company
        self.source = digest(self.sources_for(company))
        self.pending = {}
        self.stats = {"cache_hits": 0, "cache_misses": 0}

    def end(self):
        if self.token is not None:
            CURRENT.reset(self.token)
            self.token = None

    def key(self, listing):
        # Includes origin/site through source hash; never cross-tenant requisition reuse.
        return digest([self.company["id"], self.source, listing.url])

    def fingerprint(self, listing):
        return digest([listing.title, listing.location, listing.url, listing.card_text])

    def lookup(self, listing):
        entry = self.cache["entries"].get(self.key(listing))
        if (not self.config["cache_enabled"] or not entry or entry["rules"] != self.rules
                or entry["fingerprint"] != self.fingerprint(listing) or epoch(entry["expires_at"]) <= self.wall()):
            return None
        return entry

    def order(self, listings):
        fresh, aged, hits = [], [], []
        for listing in listings:
            key = self.key(listing)
            if self.lookup(listing):
                hits.append(listing)
            elif key in self.cache["entries"] and self.cache["entries"][key]["fingerprint"] != self.fingerprint(listing):
                fresh.append(listing)
            elif key in self.pending or key in self.cache["entries"]:
                aged.append(listing)
            else:
                fresh.append(listing)
        aged.sort(key=lambda item: self.pending.get(self.key(item), {}).get("since") or
                  self.cache["entries"].get(self.key(item), {}).get("evaluated_at", ""))
        # Reserve a quarter for old pending/revalidation without delaying the first new job.
        ordered = []
        while fresh or aged:
            ordered.extend(fresh[:3]); del fresh[:3]
            if aged:
                ordered.append(aged.pop(0))
        return ordered + hits

    def defer_listing(self, listing, reason="incomplete"):
        key = self.key(listing)
        if len(self.pending) >= 10000 and key not in self.pending:
            # Safety cap is visible; listing discovery next run is still authoritative.
            self.stats["pending_overflow"] = self.stats.get("pending_overflow", 0) + 1
            return
        self.pending[key] = {"since": self.pending.get(key, {}).get("since", iso(self.wall())), "reason": reason}

    def cache_detail(self, listing, detail, verdict, fingerprint, reason=""):
        key = self.key(listing)
        self.pending.pop(key, None)
        if not self.config["cache_enabled"] or not detail or not detail.ok or verdict not in ("accepted", "rejected"):
            return
        # Only the schema's public posting evidence fields, never response metadata.
        payload = {k: v for k, v in asdict(detail).items() if k != "diagnostic"}
        if verdict == "rejected":
            # A rejection keeps only its reason (Nov 2026: all sources are cached, so full
            # descriptions would grow the published file by megabytes); a hit replays it.
            payload.update(description="", strict_text="", reason=(reason or "rejected")[:2000])
        entry = {"company_id": self.company["id"], "source": self.source, "fingerprint": fingerprint,
                 "rules": self.rules, "evaluated_at": iso(self.wall()),
                 "expires_at": iso(self.wall() + self.config["cache_ttl_hours"] * 3600),
                 "title": listing.title, "detail": payload, "verdict": verdict}
        candidate = empty(CACHE_FILE)
        candidate["entries"][key] = entry
        try:
            validate(CACHE_FILE, candidate)
        except ValueError:
            self.stats["uncacheable"] = self.stats.get("uncacheable", 0) + 1
            return
        self.cache["entries"][key] = entry

    def projection(self, company, outcome, reason="", *, attempted=False):
        cfg = self.sources_for(company)
        host_records = [self.state["hosts"].get(h, {}) for h in hosts(cfg)]
        cooldown = max((r.get("cooldown_until", "") for r in host_records), default="")
        prev = self.state["companies"].get(company["id"], {})
        events = self.http.events[self.event_start:] if attempted else []
        last = self.last_stop if attempted and self.last_stop else (events[-1] if events else {})
        if attempted and outcome == "healthy" and (self.pending or self.last_stop):
            outcome, reason = "degraded", "incomplete"   # read, but some detail work is pending
        record = {"source": digest(cfg), "observed_at": iso(self.wall()),
                  "last_attempt": company.get("last_checked", "") if attempted else prev.get("last_attempt", company.get("last_checked", "")),
                  "last_success": company.get("last_success", ""), "outcome": outcome,
                  "pending": dict(self.pending) if attempted else prev.get("pending", {})}
        if attempted and events:
            record["last_attempt"] = iso(self.wall())
        self.state["companies"][company["id"]] = record
        failed_before = company.get("status") in ("failing", "broken", "needs_config")
        return {"version": 1, "company_id": company["id"], "discovery_method": ", ".join(c.get("type", "") for c in cfg if c.get("type") in ("workday", "workday_sitemap")),
                "outcome": outcome, "failure_category": last.get("failure_category") or reason,
                "http_status": last.get("http_status"), "host": last.get("host") or ", ".join(hosts(cfg)),
                "retry_after_s": last.get("retry_after_s", 0), "retry_after_valid": last.get("retry_after_valid", False),
                "cooldown_until": cooldown, "last_attempt": record["last_attempt"], "last_success": record["last_success"],
                "observed_at": record["observed_at"], "source": record["source"],
                "unresolved_failure": failed_before or outcome in ("failed", "rate_limited", "needs_config"),
                "pending": len(record["pending"]), "oldest_pending": min((p["since"] for p in record["pending"].values()), default=""),
                "overdue": not epoch(record["last_success"]) or self.wall() - epoch(record["last_success"]) >= self.config["overdue_hours"] * 3600,
                "requests": len(events), **(self.stats if attempted else {})}

    def save(self, started_at, finished_at):
        self.state["last_completed_run"] = {"run_id": self.run_id, "started_at": started_at, "finished_at": finished_at}
        for obj in (self.state, self.cache):
            obj.update(run_id=self.run_id, updated_at=finished_at)
        self.cache["entries"] = dict(sorted(((k, v) for k, v in self.cache["entries"].items()
                                             if epoch(v["expires_at"]) > self.wall()),
                                            key=lambda p: p[1]["evaluated_at"], reverse=True)[:self.config["cache_max_entries"]])
        while len(json.dumps(self.cache).encode()) > MAX_BYTES:
            self.cache["entries"].pop(next(reversed(self.cache["entries"])))
        # Never repair an unreadable safety state by silently deleting its cooldowns.
        if self.state_error:
            raise ValueError("M7 safety state unavailable; refusing publication")
        write(self.data_dir / STATE_FILE, STATE_FILE, self.state)
        write(self.data_dir / CACHE_FILE, CACHE_FILE, self.cache)
