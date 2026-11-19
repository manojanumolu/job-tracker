"""M7 safety and persistence tests. Mock transports; no network or delivery."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

import monitoring_health
import repo_sync
import scan_handoff
import scan_schedule
import scraper
import sources
from workday_alternatives import permitted
from workday_http import Controller, CURRENT, WorkdayStop, retry_after
from workday_runtime import Recovery, configuration
from workday_state import CACHE_FILE, STATE_FILE, digest, empty, epoch, iso, merge, publication_payload, validate, write

HOST = "sanofi.wd3.myworkdayjobs.com"
URL = f"https://{HOST}/wday/cxs/sanofi/SanofiCareers/jobs"
COMPANY = {"id": "sanofi", "name": "Sanofi", "url": f"https://{HOST}/SanofiCareers"}


class Clock:
    def __init__(self):
        self.t = 1800000000.0
        self.waits = []
    def now(self):
        return self.t
    def sleep(self, seconds):
        self.waits.append(seconds)
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cfg():
    out = configuration()
    out.update(mode="full", company_delay_s=0)
    return out


def control(clock, cfg, state=None):
    return Controller(state or empty(STATE_FILE), cfg, clock=clock.now, wall=clock.now, sleep=clock.sleep)


def recovery(tmp_path, clock, cfg, companies=None):
    return Recovery(tmp_path, companies or [COMPANY], scraper.sources_for, config=cfg,
                    clock=clock.now, wall=clock.now, sleep=clock.sleep)


def test_spacing_is_global_and_host_scoped_across_clients(clock, cfg):
    c = control(clock, cfg)
    times = []
    def handler(req):
        times.append(clock.now())
        return httpx.Response(200, json={})
    for url in [URL, URL, URL.replace(HOST, "pwc.wd3.myworkdayjobs.com"), URL]:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            c.request(client, "POST", url)
    # 2 s global spacing (the 429 was shared across tenants) and 2 s per host
    assert [t - times[0] for t in times] == [0, 2, 4, 6]


@pytest.mark.parametrize("value,valid", [("30", True), ("-1", False), ("nonsense", False), (None, False), ("NaN", False)])
def test_retry_after_seconds_and_invalid(value, valid, clock):
    delay, ok = retry_after(value, clock.now())
    assert ok == valid
    assert delay == (30 if valid else 0)


def test_retry_after_http_date(clock):
    from email.utils import format_datetime
    date = format_datetime(datetime.fromtimestamp(clock.now() + 90, timezone.utc), usegmt=True)
    assert retry_after(date, clock.now()) == (90, True)


def test_429_without_retry_after_is_never_retried_and_circuit_blocks_details(clock, cfg):
    c = control(clock, cfg)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429))) as client:
        with pytest.raises(WorkdayStop) as err:
            c.request(client, "POST", URL)
        assert err.value.diagnostic["http_status"] == 429
        assert c.requests == 1 and clock.waits == []      # one request, no blind retry
        with pytest.raises(WorkdayStop, match="cooldown"):
            c.request(client, "GET", URL + "/job/1")
        assert c.requests == 1
        assert epoch(c.state["hosts"][HOST]["cooldown_until"]) == clock.now() + 10800


def test_429_short_retry_after_is_honoured_exactly(clock, cfg):
    replies = iter([httpx.Response(429, headers={"Retry-After": "20"}), httpx.Response(429, headers={"Retry-After": "20"}),
                    httpx.Response(200)])
    c = control(clock, cfg)
    with httpx.Client(transport=httpx.MockTransport(lambda req: next(replies))) as client:
        c.request(client, "POST", URL)
    assert c.requests == 3 and clock.waits == [20, 20]   # what the server asked, no more, no less
    assert not c.cooling(HOST)


def test_429_retry_after_beyond_run_budget_stops_at_once(clock, cfg):
    c = control(clock, cfg)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429, headers={"Retry-After": "600"}))) as client:
        with pytest.raises(WorkdayStop):
            c.request(client, "POST", URL)
    assert c.requests == 1 and clock.waits == []
    assert epoch(c.state["hosts"][HOST]["cooldown_until"]) == clock.now() + 10800   # fallback 3 h > 600 s


def test_server_errors_keep_bounded_exponential_backoff(clock, cfg):
    c = control(clock, cfg)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503))) as client:
        with pytest.raises(WorkdayStop):
            c.request(client, "POST", URL)
    assert c.requests == 3 and clock.waits == [30, 60]


def test_existing_user_agent_is_kept_for_workday(clock, cfg):
    seen = []
    c = control(clock, cfg)
    def handler(req):
        seen.append(req.headers.get("User-Agent"))
        return httpx.Response(200, json={})
    with httpx.Client(headers=sources.HEADERS, transport=httpx.MockTransport(handler)) as client:
        c.request(client, "POST", URL)
    assert seen == [sources.HEADERS["User-Agent"]]


def test_long_retry_after_persists_no_early_trial(tmp_path, clock, cfg):
    c = control(clock, cfg)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429, headers={"Retry-After": "172800", "Set-Cookie": "secret"}))) as client:
        with pytest.raises(WorkdayStop):
            c.request(client, "POST", URL)
    assert c.requests == 1 and not clock.waits
    write(tmp_path / STATE_FILE, STATE_FILE, c.state)
    run = recovery(tmp_path, clock, cfg)
    assert run.defer_reason(COMPANY) == "cooldown"
    assert epoch(run.state["hosts"][HOST]["cooldown_until"]) == clock.now() + 172800
    assert "secret" not in json.dumps(run.state)


def test_successful_trial_closes_circuit(clock, cfg):
    replies = iter([httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(200), httpx.Response(200)])
    c = control(clock, cfg)
    with httpx.Client(transport=httpx.MockTransport(lambda req: next(replies))) as client:
        c.request(client, "POST", URL)
        c.request(client, "POST", URL)
    assert c.requests == 3 and not c.cooling(HOST) and not c.closed


def test_provider_breaker_does_not_block_healthy_sitemap(clock, cfg):
    cfg["max_retries"] = 0
    c = control(clock, cfg)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429))) as client:
        for i in range(2):   # api_breaker_hosts = 2: the limit is shared across tenants
            with pytest.raises(WorkdayStop):
                c.request(client, "POST", URL.replace(HOST, f"tenant{i}.wd1.myworkdayjobs.com"))
        with pytest.raises(WorkdayStop, match="circuit"):
            c.request(client, "POST", URL)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200))) as client:
        c.request(client, "GET", "https://jiostar.wd102.myworkdayjobs.com/JioStar/siteMap.xml")
    assert c.requests == 3


@pytest.mark.parametrize("status,category", [(403, "forbidden"), (401, "authentication"), (404, "not_found"), (302, "redirect"), (500, "server_error")])
def test_other_http_errors_are_structured_no_redirects(status, category, clock, cfg):
    cfg["max_retries"] = 0
    c = control(clock, cfg)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, headers={"Location": "http://127.0.0.1/private"}))) as client:
        with pytest.raises(WorkdayStop) as err:
            c.request(client, "GET", URL)
    assert err.value.diagnostic["failure_category"] == category
    assert c.requests == 1
    assert c.cooling(HOST) == (status != 404)


def test_request_budget_stops_without_contact(clock, cfg):
    cfg["max_requests"] = 0
    c = control(clock, cfg)
    with pytest.raises(WorkdayStop, match="budget"):
        c.request(None, "GET", URL)
    assert c.requests == 0


def listing(name="Graduate Software Engineer", job="JR12345", description=None):
    return sources.Listing(title=name, url=f"https://{HOST}/SanofiCareers/job/Hyderabad/{job}", location="Hyderabad, India",
        posting_evidence=True, fetch_detail=lambda: sources.Detail(ok=True,
            description=description or "Freshers welcome. 0-1 years of experience. Build software services and develop Python applications for our customers.",
            location="Hyderabad, India", posting_evidence=True, matched="same posting"))


def evaluate(run, item, budget=500):
    scan = scraper.Scan("Sanofi")
    run.begin(COMPANY)
    try:
        scraper._evaluate(scan, item, [budget])
    finally:
        run.end()
    return scan


def test_cache_warm_hit_preserves_jobs_evidence_and_ttl(tmp_path, clock, cfg):
    run = recovery(tmp_path, clock, cfg)
    first = evaluate(run, listing())
    assert len(first.jobs) == 1
    entry = copy.deepcopy(next(iter(run.cache["entries"].values())))
    clock.sleep(60)
    second_item = listing()
    second_item.fetch_detail = lambda: pytest.fail("warm cache fetched a detail")
    second = evaluate(run, second_item)
    assert second.jobs == first.jobs
    assert next(iter(run.cache["entries"].values())) == entry
    assert run.stats["cache_hits"] == 1
    assert scraper.merge_new_jobs(scraper.merge_new_jobs([], first.jobs), second.jobs) == []


@pytest.mark.parametrize("change", ["expiry", "rules", "title", "location", "source"])
def test_cache_invalidation_and_changed_job_reevaluation(change, tmp_path, clock, cfg):
    run = recovery(tmp_path, clock, cfg)
    evaluate(run, listing())
    item = listing()
    run.begin(COMPANY)
    try:
        if change == "expiry": clock.sleep(cfg["cache_ttl_hours"] * 3600 + 1)
        if change == "rules": run.rules = digest("new rules")
        if change == "title": item.title += " - Data"
        if change == "location": item.location = "Pune, India"
        if change == "source": run.source = digest("new source")
        assert run.lookup(item) is None
    finally:
        run.end()


def test_priority_new_changed_and_aged_fairness(tmp_path, clock, cfg):
    run = recovery(tmp_path, clock, cfg)
    evaluate(run, listing())
    run.begin(COMPANY)
    try:
        old = listing(job="JR55555")
        run.defer_listing(old)
        changed = listing(name="Graduate Data Engineer")
        new = [listing(job=f"JR1000{i}") for i in range(5)]
        ordered = run.order([old, changed, *new])
        assert ordered[0] is changed and ordered[3] is old
    finally:
        run.end()


def test_unreadable_and_budget_are_pending_not_negative_cache(tmp_path, clock, cfg):
    run = recovery(tmp_path, clock, cfg)
    item = listing()
    item.fetch_detail = lambda: sources.Detail.unreadable("HTTP 429")
    assert evaluate(run, item).unknown == 1
    assert not run.cache["entries"] and run.pending
    assert evaluate(run, listing(job="JR99999"), budget=0).pending == 1


def test_rejected_details_are_cached_but_unknown_eligibility_not(tmp_path, clock, cfg):
    run = recovery(tmp_path, clock, cfg)
    rejected = listing(description="Requires at least 5 years of professional employment building enterprise software services.")
    assert evaluate(run, rejected).rejected == 1
    assert next(iter(run.cache["entries"].values()))["verdict"] == "rejected"
    unknown = listing(name="Software Engineer", job="JR00000", description="Build software services and develop Python applications for customers in an engineering team.")
    assert evaluate(run, unknown).unknown == 1
    assert len(run.cache["entries"]) == 1 and not run.pending


def test_corrupt_cache_rebuilds_and_corrupt_safety_state_fails_closed(tmp_path, clock, cfg):
    (tmp_path / CACHE_FILE).write_text('{"broken": true}')
    run = recovery(tmp_path, clock, cfg)
    assert run.cache_error and not run.cache["entries"]
    (tmp_path / STATE_FILE).write_text('not json')
    run = recovery(tmp_path, clock, cfg)
    assert run.defer_reason(COMPANY) == "state_unavailable"
    with pytest.raises(ValueError):
        run.save(iso(clock.now()), iso(clock.now()))


def test_pending_and_cache_survive_restart(tmp_path, clock, cfg):
    run = recovery(tmp_path, clock, cfg)
    evaluate(run, listing())
    run.begin(COMPANY)
    run.defer_listing(listing(job="JR99999"), "budget")
    run.projection(COMPANY, "deferred", attempted=True)
    run.end()
    run.save(iso(clock.now()), iso(clock.now()))
    restarted = recovery(tmp_path, clock, cfg)
    restarted.begin(COMPANY)
    try:
        assert restarted.pending and restarted.lookup(listing())
    finally:
        restarted.end()


def companies(n=26):
    return [{"id": f"c{i:02}", "name": f"Company {i}", "url": f"https://tenant{i}.wd1.myworkdayjobs.com/Careers",
             "status": "active", "last_checked": iso(1700000000 + i), "last_success": iso(1700000000 + i)} for i in range(n)]


def _attempt(run, cos, chosen):
    for c in cos:
        if c["id"] in chosen:
            c["last_checked"] = iso(run.wall())
            run.state["companies"][c["id"]] = {"source": digest(scraper.sources_for(c)), "observed_at": iso(run.wall()),
                                               "last_attempt": iso(run.wall()), "last_success": iso(run.wall()),
                                               "outcome": "healthy", "pending": {}}


def test_rotation_covers_26_every_6_hours_with_half_per_window(tmp_path, clock, cfg):
    """3 h scan windows; each Workday API company is visited every 6 h, 13 per window."""
    cos = companies()
    visits = {c["id"]: [] for c in cos}
    state = None
    for window in range(8):                        # 24 h of 3 h windows
        run = recovery(tmp_path, clock, cfg, cos)
        if state:
            run.state["companies"] = state
            run.not_due = set(); run.selected = run.select(cos)
        assert len(run.selected) == 13
        assert not run.selected & run.not_due
        for cid in run.selected:
            visits[cid].append(clock.now())
        _attempt(run, cos, run.selected)
        state = run.state["companies"]
        clock.sleep(10800 + 120)                   # windows start a little late
    for cid, times in visits.items():
        assert len(times) == 4, cid                # 4 visits a day: every 6 h, never 3 h
        assert all(6 * 3600 <= b - a <= 6.2 * 3600 for a, b in zip(times, times[1:]))


def test_not_due_company_is_reported_healthy_not_deferred(tmp_path, clock, cfg):
    cos = companies(2)
    run = recovery(tmp_path, clock, cfg, cos)
    _attempt(run, cos, {"c00"})
    run.not_due = set(); run.selected = run.select(cos)
    assert "c00" in run.not_due and run.defer_reason(cos[0]) == "rotation"


def test_overdue_attempt_priority_and_cooldown_does_not_reset_success(tmp_path, clock, cfg):
    cos = companies(15)
    run = recovery(tmp_path, clock, cfg, cos)
    assert "c00" in run.selected and "c14" not in run.selected
    c = cos[0]
    before = copy.deepcopy(c)
    projection = run.projection(c, "cooling_down", "cooldown")
    assert projection["overdue"] and projection["last_success"] == before["last_success"]
    assert c == before


def test_canary_and_pause_keep_healthy_non_api_scanners(tmp_path, clock, cfg):
    cfg["mode"] = "canary"
    run = recovery(tmp_path, clock, cfg)
    assert run.defer_reason(COMPANY) == ""
    assert run.defer_reason(companies(1)[0]) == "canary_gate"
    assert run.defer_reason({"id": "jiohotstar"}) == ""
    assert run.defer_reason({"id": "amazon"}) == ""
    cfg["mode"] = "paused"
    run = recovery(tmp_path, clock, cfg)
    assert run.defer_reason(COMPANY) == "configuration"
    assert run.defer_reason({"id": "jiohotstar"}) == ""


def test_completed_marker_makes_deferred_windows_not_due():
    c = companies(1)[0]
    c["scan"] = {"run": {"recovery_version": 1, "started_at": "2026-11-18T12:02:00+00:00", "finished_at": "2026-11-18T12:20:00+00:00"}}
    now = datetime(2026, 11, 18, 12, 47, tzinfo=timezone.utc)
    assert not scan_schedule.plan("workflow_dispatch", "", {}, [c], now, mode="plan")["scan"]
    assert c["last_checked"] == iso(1700000000)


def test_state_merge_rejects_stale_success_and_preserves_other_hosts(clock, cfg):
    old = control(clock, cfg)
    old._observation(HOST, 200, "")
    earlier = copy.deepcopy(old.state)
    clock.sleep(30)
    old._observation(HOST, 429, "rate_limit", 18000, True)
    newest = old.state
    result = merge(STATE_FILE, newest, earlier)
    assert result["hosts"][HOST]["http_status"] == 429
    assert result["hosts"][HOST]["cooldown_until"] == newest["hosts"][HOST]["cooldown_until"]


def test_cache_merge_retains_newer_refresh_against_stale_pruning(tmp_path, clock, cfg):
    run = recovery(tmp_path, clock, cfg)
    evaluate(run, listing())
    remote = copy.deepcopy(run.cache)
    local = empty(CACHE_FILE)
    local["updated_at"] = iso(clock.now() - 30)
    assert merge(CACHE_FILE, remote, local)["entries"] == remote["entries"]


@pytest.mark.parametrize("mutation", ["secret", "schema", "invalid_host", "invalid_type", "oversized"])
def test_state_schema_refuses_hostile_artifacts(mutation, clock, cfg):
    c = control(clock, cfg)
    c._observation(HOST, 429, "rate_limit")
    data = c.state
    if mutation == "secret": data["hosts"][HOST]["Set-Cookie"] = "sensitive"
    if mutation == "schema": data["schema_version"] = 99
    if mutation == "invalid_host": data["hosts"]["localhost"] = data["hosts"].pop(HOST)
    if mutation == "invalid_type": data["hosts"][HOST]["failures"] = True
    if mutation == "oversized": data["run_id"] = "x" * 1000
    with pytest.raises(scan_handoff.HandoffError):
        scan_handoff._check(STATE_FILE, data)


def test_handoff_validates_all_before_copy_and_requires_same_generation(tmp_path):
    src, dst = tmp_path / "in", tmp_path / "out"
    src.mkdir(); dst.mkdir()
    for name in (STATE_FILE, CACHE_FILE):
        obj = empty(name); obj["run_id"] = name
        (src / name).write_text(json.dumps(obj))
    with pytest.raises(scan_handoff.HandoffError, match="mixed"):
        scan_handoff.take_over(src, [STATE_FILE, CACHE_FILE], dst)
    assert not list(dst.iterdir())
    with pytest.raises(scan_handoff.HandoffError, match="incomplete"):
        scan_handoff.take_over(src, [STATE_FILE], dst)


def test_publication_payload_both_or_neither(tmp_path):
    assert publication_payload(tmp_path) == {}
    write(tmp_path / STATE_FILE, STATE_FILE, empty(STATE_FILE))
    with pytest.raises(ValueError): publication_payload(tmp_path)
    write(tmp_path / CACHE_FILE, CACHE_FILE, empty(CACHE_FILE))
    assert set(publication_payload(tmp_path)) == {f"data/{STATE_FILE}", f"data/{CACHE_FILE}"}


@pytest.mark.parametrize("n,f,severity", [(44, 26, "critical"), (20, 5, "degraded"), (20, 10, "critical"), (100, 5, "degraded"), (0, 0, "unknown"), (20, 0, "healthy")])
def test_health_thresholds(n, f, severity, clock):
    cos = [{"id": str(i), "status": "failing" if i < f else "active", "last_checked": iso(clock.now()), "last_success": iso(clock.now())} for i in range(n)]
    assert monitoring_health.health(cos, datetime.fromtimestamp(clock.now(), timezone.utc))["severity"] == severity


def test_cooling_and_deferred_failures_count_once_and_unknown_never_complete(clock):
    cos = [{"status": "failing", "scan": {"recovery": {"outcome": "cooling_down", "unresolved_failure": True}}},
           {"status": "failing", "scan": {"recovery": {"outcome": "deferred", "unresolved_failure": True}}}, {"status": "unknown"}]
    result = monitoring_health.health(cos)
    assert result["failures"] == 2 and result["severity"] == "critical" and not result["complete"]


def test_actions_summary_does_not_emit_names_errors_or_headers(tmp_path, monkeypatch):
    path = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(path))
    monitoring_health.report_actions([{"name": "::error::secret", "status": "failing", "status_reason": "Set-Cookie:secret"}])
    assert "secret" not in path.read_text() and "critical" in path.read_text()


def test_alternative_policy_requires_verified_permitted_evidence(clock):
    config = {"site": f"https://{HOST}/SanofiCareers"}
    assert not permitted(config)
    config["access_policy"] = {"terms_permit_automation": True, "verified_at": iso(clock.now()), "terms_url": "https://example.org/terms",
        "robots_text": f"User-agent: *\nAllow: /\nSitemap: https://{HOST}/SanofiCareers/siteMap.xml"}
    now = datetime.fromtimestamp(clock.now(), timezone.utc)
    assert permitted(config, now)
    config["access_policy"]["robots_text"] = "User-agent: *\nDisallow: /"
    assert not permitted(config, now)


def test_bootstrap_retains_saved_429_and_missing_state_after_m7_blocks(tmp_path, clock, cfg):
    c = {**COMPANY, "status": "failing", "status_reason": "HTTP 429"}
    run = recovery(tmp_path, clock, cfg, [c])
    assert run.defer_reason(c) == "cooldown"
    c["scan"] = {"recovery": {"version": 1}}
    run = recovery(tmp_path, clock, cfg, [c])
    assert run.state_error == "state_unavailable"


def test_workflow_explicitly_transfers_both_state_files():
    text = (Path(__file__).parents[1] / ".github/workflows/check_jobs.yml").read_text("utf-8")
    for name in (STATE_FILE, CACHE_FILE):
        assert f"jt-data/data/{name}" in text
    assert "seen_jobs.json workday_scan_state.json workday_verdict_cache.json" in text


def test_run_all_persists_generation_and_does_not_touch_deferred_timestamps(tmp_path, clock, cfg, monkeypatch):
    import config_store
    cos = [{**COMPANY, "status": "active", "last_checked": iso(clock.now() - 3600), "last_success": iso(clock.now() - 3600)},
           {**companies(1)[0], "status": "failing", "status_reason": "HTTP 429"}]
    cfg.update(mode="canary", canary_company_ids=["sanofi"])
    old = copy.deepcopy(cos[1])
    monkeypatch.setattr(config_store, "load_companies", lambda: cos)
    monkeypatch.setattr(config_store, "load_seen_jobs", lambda: [])
    monkeypatch.setattr(config_store, "save_companies", lambda data, **kw: (tmp_path / "companies.json").write_text(json.dumps(data)))
    monkeypatch.setattr(config_store, "save_seen_jobs", lambda data, **kw: (tmp_path / "seen_jobs.json").write_text(json.dumps(data)))
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    monkeypatch.setattr(scraper, "_now_iso", lambda: iso(clock.now()))
    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"jobPostings": [{"title": "Graduate Software Engineer", "locationsText": "Hyderabad, India", "externalPath": "/job/Hyderabad/Graduate_JR12345"}]})
        return httpx.Response(200, json={"jobPostingInfo": {"title": "Graduate Software Engineer", "location": "Hyderabad", "country": "India",
                "jobDescription": "Freshers welcome. 0-1 years of experience. Build Python software and services for our customers."}})
    real_client = httpx.Client
    monkeypatch.setattr(scraper.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    run = recovery(tmp_path, clock, cfg, cos)
    found = scraper.run_all(recovery=run)
    assert len(found) == 1 and cos[0]["status"] == "active"
    assert cos[1]["last_checked"] == old["last_checked"] and cos[1]["last_success"] == old["last_success"]
    assert cos[1]["scan"]["recovery"]["outcome"] == "deferred"
    assert cos[1]["scan"]["recovery"]["unresolved_failure"]
    dest = tmp_path / "handoff"
    dest.mkdir()
    scan_handoff.take_over(tmp_path, ["companies.json", "seen_jobs.json", STATE_FILE, CACHE_FILE], dest)
    assert len(publication_payload(dest)) == 2
    assert scan_schedule.last_scan(cos) == datetime.fromtimestamp(1800000000, timezone.utc)


def test_detail_429_stops_real_adapter_and_preserves_pending(tmp_path, clock, cfg, monkeypatch):
    cfg["max_retries"] = 0
    run = recovery(tmp_path, clock, cfg)
    requests = []
    def handler(req):
        requests.append(req)
        if req.method == "POST":
            return httpx.Response(200, json={"jobPostings": [
                {"title": "Graduate Software Engineer", "locationsText": "Hyderabad, India", "externalPath": f"/job/Hyderabad/Graduate_JR1000{i}"}
                for i in range(5)]})
        return httpx.Response(429, headers={"Retry-After": "600"})
    real_client = httpx.Client
    monkeypatch.setattr(scraper.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    run.begin(COMPANY)
    try:
        scan = scraper.scan_company(COMPANY)
    finally:
        run.end()
    assert scan.status == "failing" and not scan.jobs
    assert len(requests) == 2 and len(run.pending) == 5
    assert run.last_stop["http_status"] == 429 and run.http.cooling(HOST)


def test_forbidden_host_requires_review_even_after_cooldown(clock, cfg):
    cfg["max_retries"] = 0
    c = control(clock, cfg)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(403))) as client:
        with pytest.raises(WorkdayStop): c.request(client, "GET", URL)
        clock.sleep(86400 * 2)
        restarted = control(clock, cfg, c.state)
        with pytest.raises(WorkdayStop, match="configuration"):
            restarted.request(client, "GET", URL)
        assert restarted.requests == 0


def test_circuit_state_survives_budget_exhaustion_during_retry(clock, cfg):
    cfg["max_requests"] = 1
    c = control(clock, cfg)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429))) as client:
        with pytest.raises(WorkdayStop): c.request(client, "POST", URL)
    assert c.cooling(HOST) and c.state["hosts"][HOST]["http_status"] == 429


def test_stale_company_scan_never_overwrites_newer_result():
    remote = {**COMPANY, "status": "failing", "last_checked": "2026-11-18T12:00:00+00:00"}
    local = {**COMPANY, "status": "active", "last_checked": "2026-11-18T09:00:00+00:00"}
    assert repo_sync.merge_companies([remote], [local]) == [remote]


def test_publisher_explicit_merge_preserves_cooldown_on_push_retry(tmp_path, clock, cfg, monkeypatch):
    import subprocess
    (tmp_path / "data").mkdir()
    c = control(clock, cfg)
    c._observation(HOST, 200, "")
    local = copy.deepcopy(c.state)
    clock.sleep(30)
    c._observation(HOST, 429, "rate_limit")
    remote = c.state
    monkeypatch.setattr(repo_sync, "_remote_json", lambda *args: remote)
    pushes = []
    def git(*args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            return subprocess.CompletedProcess(args, 1 if len(pushes) == 1 else 0, "", "conflict")
        return subprocess.CompletedProcess(args, 1 if args[:2] == ("diff", "--cached") else 0, "", "")
    monkeypatch.setattr(repo_sync, "_git", git)
    assert repo_sync.publish("test", {f"data/{STATE_FILE}": local}, cwd=tmp_path)
    saved = json.loads((tmp_path / "data" / STATE_FILE).read_text())
    assert saved["hosts"][HOST]["http_status"] == 429 and len(pushes) == 2


def test_failed_state_validation_prevents_initial_alert_publication(tmp_path, monkeypatch):
    import alerts
    import config_store
    monkeypatch.setattr(config_store, "DATA", tmp_path)
    (tmp_path / STATE_FILE).write_text("invalid")
    monkeypatch.setattr(alerts, "load_seen_jobs", lambda: [])
    monkeypatch.setattr(alerts, "load_companies", lambda: [])
    monkeypatch.setattr(alerts, "load_settings", lambda: {})
    sent, published = [], []
    assert alerts.run(send=lambda *a: sent.append(a), publisher=lambda *a: published.append(a)) == 1
    assert not sent and not published


def test_sitemap_index_support_and_same_identity():
    site = "https://jiostar.wd102.myworkdayjobs.com/JioStar"
    requests = []
    def handler(req):
        requests.append(str(req.url))
        if req.url.path.endswith("siteMap.xml"):
            return httpx.Response(200, text=f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>{site}/part.xml</loc></sitemap></sitemapindex>')
        return httpx.Response(200, text=f'<urlset><url><loc>{site}/job/Hyderabad/Graduate-Engineer_JR12345</loc></url></urlset>')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sources.workday_sitemap_listings(client, {"site": site})
    assert len(requests) == 2 and result.listings[0].job_id == "JR12345"


@pytest.mark.parametrize("body", [
    '<!DOCTYPE x [<!ENTITY x "secret">]><urlset/>',
    '<sitemapindex><sitemap><loc>http://127.0.0.1/private</loc></sitemap></sitemapindex>',
    '<sitemapindex><sitemap><loc>https://jiostar.wd102.myworkdayjobs.com/JioStar/siteMap.xml</loc></sitemap></sitemapindex>',
    '<urlset><url><loc>https://evil.example/job/Hyderabad/JR12345</loc></url></urlset>',
])
def test_hostile_sitemap_never_follows_untrusted_url(body):
    asked = []
    def handler(req):
        asked.append(req)
        return httpx.Response(200, text=body)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(sources.SourceError):
            sources.workday_sitemap_listings(client, {"site": "https://jiostar.wd102.myworkdayjobs.com/JioStar"})
    assert len(asked) == 1


def test_handoff_diagnostics_reject_header_injection(tmp_path, clock, cfg):
    run = recovery(tmp_path, clock, cfg)
    projection = run.projection(COMPANY, "deferred", "rotation")
    projection["Set-Cookie"] = "secret"
    with pytest.raises(scan_handoff.HandoffError):
        scan_handoff._check("companies.json", [{**COMPANY, "scan": {"recovery": projection}}])


def test_new_global_marker_cannot_overwrite_newer_portal_attempt():
    remote = {**COMPANY, "status": "failing", "last_checked": "2026-11-18T12:30:00+00:00"}
    local = {**COMPANY, "status": "active", "last_checked": "2026-11-18T12:00:00+00:00", "scan": {"run": {
        "recovery_version": 1, "started_at": "2026-11-18T12:00:00+00:00", "finished_at": "2026-11-18T12:40:00+00:00"}}}
    merged = repo_sync.merge_companies([remote], [local])[0]
    assert merged["last_checked"] == remote["last_checked"] and merged["status"] == "failing"
    assert merged["scan"]["run"] == local["scan"]["run"]


PWC = {"id": "pwc", "name": "PwC", "url": "https://pwc.wd3.myworkdayjobs.com/Global_Experienced_Careers"}


def throttled_fleet(clock):
    """Production on 18 Nov 2026: every Workday API company last saw HTTP 429."""
    seen = iso(clock.now() - 600)
    return [{**c, "status": "failing", "status_reason": "HTTP 429", "last_checked": seen, "last_success": iso(clock.now() - 3 * 86400)}
            for c in [COMPANY, PWC, *companies(4)]]


def test_auto_mode_probes_only_the_canary_while_throttled(tmp_path, clock, cfg):
    cfg["mode"] = "auto"
    cos = throttled_fleet(clock)
    clock.sleep(4 * 3600)                                   # bootstrap cooldown has expired
    run = recovery(tmp_path, clock, cfg, cos)
    assert run.mode == "canary" and run.probe_ids == {"sanofi", "pwc"}
    assert run.selected == {"sanofi", "pwc"}
    assert {c["id"]: run.defer_reason(c) for c in cos[2:]} == {c["id"]: "canary_gate" for c in cos[2:]}


def test_auto_mode_promotes_after_canary_success_and_demotes_on_new_429(tmp_path, clock, cfg):
    cfg["mode"] = "auto"
    cos = throttled_fleet(clock)
    clock.sleep(4 * 3600)
    run = recovery(tmp_path, clock, cfg, cos)
    _attempt(run, cos, {"sanofi"})                         # canary scanned healthily after the 429s
    write(tmp_path / STATE_FILE, STATE_FILE, run.state)
    clock.sleep(3 * 3600)
    run = recovery(tmp_path, clock, cfg, cos)
    assert run.mode == "full"
    assert {"c00", "c01", "c02", "c03", "pwc"} <= run.selected   # everyone else is now due
    # a fresh 429 anywhere puts the fleet back behind the canary
    run.state["hosts"]["tenant0.wd1.myworkdayjobs.com"] = {**run.state["hosts"]["tenant0.wd1.myworkdayjobs.com"],
                                                          "observed_at": iso(clock.now())}
    write(tmp_path / STATE_FILE, STATE_FILE, run.state)
    clock.sleep(60)
    assert recovery(tmp_path, clock, cfg, cos).mode == "canary"


def test_auto_mode_projection_alone_never_promotes(tmp_path, clock, cfg):
    """A canary that was only projected (not contacted) cannot prove the limit lifted."""
    cfg["mode"] = "auto"
    cos = throttled_fleet(clock)
    run = recovery(tmp_path, clock, cfg, cos)
    run.projection(cos[0], "healthy")                       # no requests were made
    write(tmp_path / STATE_FILE, STATE_FILE, run.state)
    clock.sleep(4 * 3600)
    assert recovery(tmp_path, clock, cfg, cos).mode == "canary"


def test_auto_mode_falls_back_to_oldest_company_when_canary_is_gone(tmp_path, clock, cfg):
    cfg["mode"] = "auto"
    cos = throttled_fleet(clock)[2:]
    cos[3]["last_checked"] = iso(clock.now() - 7200)
    clock.sleep(4 * 3600)
    run = recovery(tmp_path, clock, cfg, cos)
    assert run.mode == "canary" and run.probe_ids == {cos[3]["id"]}


def test_auto_mode_without_any_throttle_is_full(tmp_path, clock, cfg):
    cfg["mode"] = "auto"
    assert recovery(tmp_path, clock, cfg, companies(3)).mode == "full"


def test_bootstrap_cooldown_counts_from_the_recorded_429(tmp_path, clock, cfg):
    cos = throttled_fleet(clock)                            # 429 recorded 10 min ago
    run = recovery(tmp_path, clock, cfg, cos)
    until = epoch(run.state["hosts"][HOST]["cooldown_until"])
    assert until == clock.now() - 600 + 3 * 3600
    assert run.defer_reason(cos[0]) == "cooldown"


def test_shipped_configuration_is_auto_6h_and_paced():
    c = configuration()
    assert c["mode"] == "auto" and c["revisit_hours"] == 6 and c["batch_size"] == 13
    assert c["global_spacing_s"] >= 2 and c["host_spacing_s"] >= 2 and c["api_breaker_hosts"] == 2


def _wire_run_all(tmp_path, clock, monkeypatch, cos, handler):
    import config_store
    monkeypatch.setattr(config_store, "load_companies", lambda: cos)
    monkeypatch.setattr(config_store, "load_seen_jobs", lambda: [])
    monkeypatch.setattr(config_store, "save_companies", lambda data, **kw: (tmp_path / "companies.json").write_text(json.dumps(data)))
    monkeypatch.setattr(config_store, "save_seen_jobs", lambda data, **kw: (tmp_path / "seen_jobs.json").write_text(json.dumps(data)))
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    monkeypatch.setattr(scraper, "_now_iso", lambda: iso(clock.now()))
    real_client = httpx.Client
    monkeypatch.setattr(scraper.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))


TEAMS = ["Payments", "Search", "Platform"]


def _graduate_jobs(n):
    def handler(req):
        handler.calls += 1
        if req.method == "POST":
            return httpx.Response(200, json={"total": n, "jobPostings": [
                {"title": f"Graduate Software Engineer {TEAMS[i]}", "locationsText": "Hyderabad, India", "externalPath": f"/job/Hyderabad/Graduate_JR{i:05}"}
                for i in range(n)]})
        i = int(req.url.path.rsplit("JR", 1)[1])
        return httpx.Response(200, json={"jobPostingInfo": {"title": f"Graduate Software Engineer {TEAMS[i]}", "location": "Hyderabad", "country": "India",
                "jobDescription": "Freshers welcome. 0-1 years of experience. Build Python software and services for our customers."}})
    handler.calls = 0
    return handler


def test_second_window_within_revisit_makes_no_requests_and_stays_healthy(tmp_path, clock, cfg, monkeypatch):
    cos = [{**COMPANY, "status": "active", "last_checked": iso(clock.now() - 7 * 3600), "last_success": iso(clock.now() - 7 * 3600)}]
    handler = _graduate_jobs(3)
    _wire_run_all(tmp_path, clock, monkeypatch, cos, handler)
    scraper.run_all(recovery=recovery(tmp_path, clock, cfg, cos))
    first = handler.calls
    assert first > 0 and cos[0]["scan"]["recovery"]["outcome"] == "healthy"
    checked = cos[0]["last_checked"]
    clock.sleep(3 * 3600)                                   # next 3 h window: not due
    scraper.run_all(recovery=recovery(tmp_path, clock, cfg, cos))
    assert handler.calls == first                           # no Workday request at all
    assert cos[0]["scan"]["recovery"]["outcome"] == "healthy" and cos[0]["status"] == "active"
    assert cos[0]["scan"]["recovery"]["failure_category"] == ""
    assert cos[0]["last_checked"] == checked
    clock.sleep(3 * 3600)                                   # 6 h: due again, details now cached
    scraper.run_all(recovery=recovery(tmp_path, clock, cfg, cos))
    assert handler.calls == first + 1                       # one listing POST, zero detail GETs
    assert cos[0]["scan"]["recovery"]["cache_hits"] == 3


def test_budget_limited_scan_is_degraded_not_failed(tmp_path, clock, cfg, monkeypatch):
    cfg["max_requests"] = 3                                 # listing + 2 details, 3 postings
    cos = [{**COMPANY, "status": "active", "last_checked": iso(clock.now() - 7 * 3600), "last_success": iso(clock.now() - 7 * 3600)}]
    _wire_run_all(tmp_path, clock, monkeypatch, cos, _graduate_jobs(3))
    found = scraper.run_all(recovery=recovery(tmp_path, clock, cfg, cos))
    rec = cos[0]["scan"]["recovery"]
    assert len(found) == 2 and cos[0]["status"] == "active"
    assert rec["outcome"] == "degraded" and rec["pending"] == 1 and not rec["unresolved_failure"]
    assert monitoring_health.company_health(cos[0])["unavailable"]   # still shown as incomplete coverage


def test_sitemap_throttle_does_not_gate_the_api_fleet(tmp_path, clock, cfg):
    cfg["mode"] = "auto"
    run = recovery(tmp_path, clock, cfg, companies(2))
    run.state["hosts"]["jiostar.wd102.myworkdayjobs.com"] = {"observed_at": iso(clock.now()), "cooldown_until": iso(clock.now() + 3600),
        "failures": 1, "http_status": 429, "failure_category": "rate_limit", "retry_after_s": 0, "retry_after_valid": False}
    write(tmp_path / STATE_FILE, STATE_FILE, run.state)
    assert recovery(tmp_path, clock, cfg, companies(2)).mode == "full"


def test_cooldown_expiring_mid_run_is_admitted_not_lost_to_rotation(tmp_path, clock, cfg):
    """19 Nov 00:03: selection ran 76 s before the canary cooldown ended; the
    canary must be probed when the loop reaches it, not deferred for 3 h."""
    cfg["mode"] = "auto"
    cos = throttled_fleet(clock)                       # 429s recorded 10 min ago
    clock.sleep(3 * 3600 - 600 - 76)                   # 76 s before cooldown ends
    run = recovery(tmp_path, clock, cfg, cos)
    assert run.selected == set() and run.defer_reason(cos[0]) == "cooldown"
    clock.sleep(300)                                   # loop reaches Sanofi after expiry
    assert run.defer_reason(cos[0]) == "" and "sanofi" in run.selected
    assert run.defer_reason(cos[2]) == "canary_gate"   # non-canary still waits


def test_mid_run_admission_respects_batch_size(tmp_path, clock, cfg):
    cfg.update(mode="full", batch_size=1)
    cos = companies(3)
    run = recovery(tmp_path, clock, cfg, cos)
    assert len(run.selected) == 1
    late = next(c for c in cos if c["id"] not in run.selected)
    assert run.defer_reason(late) == "rotation"
