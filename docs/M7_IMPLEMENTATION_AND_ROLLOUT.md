# M7 Workday recovery: root cause, fix, rollout and status

Branch `feature/m7-workday-recovery-plan`: `da87859` (original M7) + the
recovery commits listed in [Deployment](#deployment-and-workflow-status).
`docs/M7_WORKDAY_MONITORING_RECOVERY_PLAN.md` is the historical
specification; this document is the record of what was actually found,
changed, tested and verified.

## Actual root cause

Established from the production data branch history (`data/companies.json`
on `main`, every scan since October) and the GitHub Actions run list. These
are recorded facts, not estimates:

| Period | Workday API companies | Workday requests per 3 h scan | Result |
|---|---:|---:|---|
| up to 2 Nov | 3 | ~290 | healthy |
| 3 Nov → 15 Nov | 25–27 | ~1,650–1,830 | healthy for ~12 days |
| 16 Nov 00:06 UTC | 26 | ~1,750 | first 429 (Salesforce, `wd12`) |
| 16 Nov 09:05 UTC onwards | 26 | 26–52 (first POST each, all 429) | **all 26 tenants HTTP 429, every scan** |

Per-scan volume = listing POSTs (`ceil(postings/20)` + 1–2 facet requests)
+ detail GETs (`details_read`), taken from each company's saved `scan`
counters. For example, the last fully successful scan (16 Nov 06:02–06:23)
read 4,510 postings with 289 listing POSTs and 1,463 detail GETs in ~20
minutes, unpaced.

1. **Request volume.** Every 3 h scan re-downloaded the detail page of every
   India candidate posting of every Workday tenant (~1,460), whether or not
   it had changed: ~14,000 Workday CXS requests per day from GitHub-hosted
   runners for ~12 days. No code or schedule change preceded the trip
   (interval 3 h since 4 Nov; no Workday request-path change since 13 Nov).
   The 429s began on one Workday data centre and within nine hours covered
   all 26 tenants on `wd1/wd3/wd5/wd12` simultaneously. That points to a limit
   shared across tenants (per client network/identity), not per company.
2. **No backoff, so the block was perpetuated.** The pre-M7 scanner called
   every tenant again every 3 h, with no cooldown, no memory of the 429 and
   no Retry-After handling.
3. **The failure was visible but did not alert.** Each company showed
   `failing — HTTP 429` on the dashboard, and the run summary said
   `portals_failed: 26`. Every Check Jobs run still concluded `success`,
   because portal failures deliberately do not fail the workflow. So 26 of 44
   companies (59%) had no successful discovery from 16 Nov 06:16 UTC until
   this recovery.
4. **The fix was never deployed.** M7 (`da87859`) was pushed to a feature
   branch with no pull request. Production ran the pre-M7 scanner throughout.
   The "35 failed" full-suite result was from an incomplete local
   environment (missing Bash/PDF/browser dependencies). CI's full suite on
   `da87859` passed (run 44384045885).

Things that were **not** the cause, checked directly:

* Scheduling: Check Jobs ran every 3 h window on time (00:02, 03:02, …
  18:02 UTC), ~9 min each. The Scan scheduler chain was alive. There were
  no duplicate scans per window.
* Publication: every scan's artifact was uploaded, handed over and
  published (data commits every 3 h, `[skip ci]`).
* Non-Workday sources: all 18 (including JioStar's Workday sitemap on the
  same `myworkdayjobs.com` infrastructure) succeeded on every run. The limit
  applies to the CXS API path.

## Defects fixed in M7 before deployment

`da87859` already provided pacing, persistent cooldowns, verdict caching,
rotation, validated four-file handoff and health diagnostics. Measured
against the recovery requirements, it still had these defects, all fixed:

| Defect in `da87859` | Effect | Fix |
|---|---|---|
| A 429 was retried twice (30 s, 60 s) even without Retry-After | 3 requests into an active throttle per host per run | A 429 is retried only with a valid Retry-After that fits the 120 s retry budget, waiting exactly that long. Otherwise one request, then a durable cooldown. (5xx/network errors keep bounded 30 s/60 s backoff.) |
| `canary` mode had no exit | The other 24 throttled companies deferred indefinitely until someone hand-edited config | New default mode `auto` (below): canary-gated automatic promotion and demotion |
| Workday requests sent a new User-Agent | A throttled client presenting a different identity | The tracker's existing User-Agent is kept |
| Bootstrap cooldown counted from deploy time | Cooldown later than the evidence warrants | Counted from the recorded 429 (`last_checked`) |
| Rotation of 9 companies per window with no revisit floor; 24 h cache TTL | ~1,460 detail re-reads/day of unchanged postings | 6 h per-company revisit; 72 h verdict TTL (fingerprint, rules and source changes still invalidate at once) |
| Global spacing 1 s; cross-host breaker at 3 hosts | Up to 1 req/s into a limit shared across tenants | 2 s global and per-host spacing (≤0.5 req/s); stop all API requests after 2 throttled hosts in a run |
| Partial scans (own request budget, cross-host circuit, 404'd posting, pagination cap) marked `failing` | Healthy-but-partial companies inflated failure counts | Stay `active` with a pending count; reported as **degraded**. A real throttle or error mid-scan is still `failing`. |
| A not-yet-due company would be reported as "Deferred" | Healthy companies shown as incomplete every other window | Reported `healthy` with the date of its real last success |
| No per-run record of Workday activity | Couldn't tell from Actions whether Workday was contacted | Run summary line: mode, requests sent, selected, not-due, throttled and cooling hosts |

Files: `src/workday_http.py`, `src/workday_runtime.py`,
`src/workday_state.py`, `src/scraper.py`, `src/monitoring_health.py`,
`streamlit_app.py`, `config/workday_recovery.json`,
`tests/test_workday_recovery.py`, `docs/MONITORING.md`, this document.

## Monitoring interval: 6 h for Workday API, 3 h for everything else

Steady-state Workday CXS requests, from the measured per-company counters
(289 listing POSTs and ~1,460 detail candidates per full pass over the 26
companies):

| Option | Listing POSTs/day | Detail GETs/day | Total/day | vs. what tripped the limit |
|---|---:|---:|---:|---:|
| Old: 3 h, all companies, no cache | 2,312 | 11,700 | **~14,000** | 1× (tripped) |
| 3 h revisit + 72 h cache | 2,312 | ~560 | ~2,900 | 21% |
| **6 h revisit + 72 h cache (chosen)** | 1,156 | ~560 | **~1,700** | **12%** |
| 12 h revisit + 72 h cache | 578 | ~560 | ~1,150 | 8% |

Detail GETs/day = 1,460 cached verdicts revalidated once per 72 h (~490)
plus new or visibly changed postings. That second figure is an *estimate*
(~5% of candidates a day, ~70), because the data branch does not keep
per-posting history for rejected Workday postings. The run summary's
request count will replace the estimate with real numbers. Peak rate is
capped at 0.5 req/s (it was ~1.5 req/s unpaced).

Decision:

* **6 h, not 3 h.** At a 3 h revisit, listing pages alone would be 80% of
  the old listing volume, and the cache cannot reduce listing reads. A 6 h
  revisit halves them, for ~88% less traffic in total.
* **6 h, not 12 h.** 12 h saves only another ~550 requests/day (the cached
  detail traffic doesn't scale with interval) and halves freshness. It would
  only be justified if the limit trips again at ~1,700/day: the auto-demotion
  and escalating cooldowns would make that visible.
* **Non-Workday stays at 3 h.** The 18 non-Workday sources have no throttling
  evidence (healthy on every run, including through the outage). Moving the
  global interval would sacrifice their freshness for nothing. The admin
  setting `scan_interval_hours` stays 3.

Each 3 h window: non-Workday sources and JioStar first, then up to 13 Workday
API companies (oldest attempt first, sequential, 10 s between companies),
~210 requests (~7 min) in steady state. Companies attempted within the last
5 h (6 h minus 1 h slack for late scan starts) send no request. A company in
cooldown is not selected; when its cooldown ends it is the oldest attempt,
so it goes first. Per-run hard limits are 600 Workday requests and 30 min of
Workday time, so the cold-cache first sweep after recovery (~870 requests for
13 companies) spreads over ~3 windows. Pending details persist and new
postings go first. Tested in
`test_rotation_covers_26_every_6_hours_with_half_per_window`: over 24 h of
late-starting 3 h windows, each of 26 companies is visited exactly 4 times,
6.0–6.2 h apart.

## Rate-limit behaviour (as deployed)

| Situation | Behaviour |
|---|---|
| 429 without Retry-After, or Retry-After > remaining 120 s retry budget | 1 request; host cooldown 3 h (×2 per consecutive failure, max 24 h; a longer valid Retry-After wins); host closed for the rest of the run |
| 429 with Retry-After ≤ budget | Waits exactly Retry-After, then retries (at most 2 retries) |
| 5xx / timeout / network | 30 s then 60 s backoff within the 120 s budget, then cooldown |
| 2 distinct hosts throttled in one run | No further Workday API requests that run (sitemaps unaffected) |
| 401 / 403 / redirect | Host needs configuration review; never retried automatically, even after cooldown |
| Host cooling down | No request; company shown "Cooling down until …" |
| Mode `auto`, newest 429 newer than the canary's last real healthy scan | Only Sanofi and PwC (or, if neither can be probed, the single oldest company) are contacted; the rest show "waiting for the Workday canary" |
| Canary scans healthy | Next run returns to full rotation automatically |
| Any new 429 in full rotation | Next run is canary-only again |

Cooldowns, per-company attempts, pending postings and verdicts are stored in
`data/workday_scan_state.json` and `data/workday_verdict_cache.json` on the
data branch. They are handed from the scan job to the publish job as
schema-validated JSON with a matching run generation, and merged per host,
company and entry on publish, so the newer record wins. Corrupt or missing
safety state after an M7 run stops Workday requests and refuses publication
rather than forgetting a cooldown. No robots.txt, access restriction or rate
limit is bypassed: there is no alternate identity, browser fallback, proxy
or unofficial endpoint.

`JT_WORKDAY_MODE=paused` (or `"mode": "paused"` in the config) is the
emergency stop for the Workday API. Other scanners continue and state is
kept.

## Test results

| Run | Code | Passed | Failed | Skipped |
|---|---|---:|---:|---:|
| CI full suite (authoritative, Python 3.12 + Chromium), run 44394841652 | `3ff3987` (merged head) | **3,647** | **0** | 39 |
| CI full suite, run 44394415492 | `858de08` | pass | 0 | — |
| Local full suite (Python 3.12, hashed lockfile) | `858de08` + WIP | 3,646 | 0 | 39 |
| Focused M7 (`tests/test_workday_recovery.py`) | `3ff3987` | 81 | 0 | 0 |
| Focused alerts / workflow / JioStar / polish | `3ff3987` | 168 (incl. M7) | 0 | 0 |

All 39 skips are Firestore/Auth-emulator tests (`test_firestore_emulator.py`,
`test_ledger_dryrun.py`) that need emulators not present on CI runners. They
are unrelated to M7 and not a deployment blocker. The earlier "3,579 passed,
35 failed" came from an incomplete local environment (no Bash, PDF libraries
or browser), not from the code: CI's suite on `da87859` already passed. No
test was skipped, weakened or deleted. Eight M7 tests that pinned the old
behaviour (blind 429 retries, 9-company batches, 1 s spacing, 24 h TTL,
`configuration` as the canary reason) were updated to assert the new
behaviour exactly. Fifteen new tests cover: no blind 429 retry, a long Retry-After stopping at once, an exact
Retry-After honoured, 5xx backoff, the User-Agent kept, auto promotion and
demotion, projection-only never promoting, the oldest-company fallback probe,
sitemap 429s not gating the API fleet, no throttle meaning full rotation, the shipped configuration, the bootstrap anchor, 26 companies
visited every 6 h over 24 h, a not-due window making zero requests, a cached
revisit making one listing request and zero detail requests, and budget-limited
scans counting as degraded.

## Canary results (production, GitHub Actions, real Workday)

The canary ran through the production scanner and workflow handoff in GitHub
Actions. This sandbox cannot reach Workday (proxy-denied), so no local live
call was made.

| When (UTC, 19 Nov) | Run | Sanofi | PwC | Other 24 API cos | Notes |
|---|---|---|---|---|---|
| 18 Nov 21:19 | 1523 (first M7 run) | cooling (bootstrap from 21:04 429) | cooling | 0 requests, waiting for canary | state + cache published (`c4bda05`) |
| 00:02 | scheduled | not probed | not probed | 0 requests | defect 1 below |
| 00:14 | 1526 (controlled) | not probed | not probed | 0 requests | defect 2 below |
| **01:30** | **controlled, `94e1059`** | **1 request → HTTP 429**, no Retry-After | **1 request → HTTP 429**; second site not requested | **0 requests** | cooldown escalated to 6 h, until 07:39 |

**Result: the Workday limit is still in force.** As of 19 Nov 01:39 UTC it
had lasted ~2.5 days, and ~4.5 h had passed since the old scanner's last
poke. Verified in production: one request per throttled host, no blind
retry, a durable escalating cooldown (`failures: 2` → 6 h), the other 24
held back with no requests, and JioStar and the 17 non-Workday sources
unaffected. No canary company has recovered, and none is reported as
recovered.

Defects found by the canary and fixed (each with a regression test that
fails on the old code):

1. Selection was a snapshot at run start: the 00:02 scan selected 76 s
   before the cooldown ended. Cooldown-expired companies are now admitted
   mid-run (PR 22).
2. The 6 h revisit also held back *failed* companies, measured from the
   pre-M7 429, so the canary would have waited until 02:04 instead of 00:04.
   Retry after failure is now governed only by the cooldown (PR 22).
3. A stale failure projection could keep a recovered company "failing" on
   the dashboard (PR 22).
4. PwC's second-site cooldown stop masked its first site's real 429 in the
   diagnostics (`failed/cooldown` instead of `rate_limited/429`) (PR 23).

What happens next, automatically. The 06:02 scan's selection runs before
07:39, so the next canary probe is the **09:02 UTC** scan.

* A healthy canary → the next run is full rotation: 13 per window, oldest
  first, all 26 covered every 6 h, the cold first sweep spread over ~3
  windows by the 600-request cap.
* 429 again → 1 request per host, cooldown 12 h, then 24 h (maximum). The
  others stay gated and visible.

## Deployment and workflow status

| Item | Status |
|---|---|
| PR 21 (M7 + recovery fixes) | merged into `main` as `3fdc1ec`, 18 Nov 21:19 UTC; CI 3,647 passed / 0 failed / 39 skipped |
| PR 22 (canary timing, retry-after-failure, stale projections) | merged as `94e1059`, 19 Nov 01:29 UTC; CI **3,653 passed / 0 failed / 39 skipped** |
| PR 23 (PwC diagnostic masking, this record) | see the PR |
| Deployment mechanism | Check Jobs checks out `main` on every run; there is no separate deploy step. The Streamlit app reads `main`. |
| Check Jobs workflow | `active`; cron `17,47 * * * *` (plan tick) and `20 */6 * * *` (personal alerts) |
| Scan scheduler chain | `active`, self-dispatching; dispatches plan at each 3 h window (00:02, 03:02, …) |
| Scan interval | 3 h for all scanners (`scan_interval_hours: 3`, unchanged); Workday API companies revisited every 6 h within it |
| M7 state persistence | verified across 4 consecutive M7 runs (18 Nov 21:19; 19 Nov 00:02, 00:14, 01:30): each read the previous state, carried cooldowns forward, published a matching generation, and never reset state |
| Emails | none sent by any verification run ("No new alerts"; shared ledger unchanged at 42 sent, 0 uncertain; personal alerts `off_jobs: 0`) |

## Company-level recovery status (19 Nov 01:39 UTC, after the canary; superseded by the per-DC section below)

| State | Count | Companies |
|---|---:|---|
| Healthy | 18 | MetLife, Avalara, Accenture, NPCI (Zoho), Amazon, Microsoft, Google, Infosys, Deloitte, Qualcomm, JioStar (Workday sitemap), AMD, Netflix, Oracle, IBM, ADP, ServiceNow, Swiggy |
| Degraded (partial) | 0 | — |
| Throttled (HTTP 429, cooling until 07:39) | 2 | Sanofi, PwC (canary) |
| Deferred, waiting for the canary | 24 | PokerStars, Salesforce, Micron, NVIDIA, Wells Fargo, Novartis, Carrier, Synchrony, Broadridge, Invesco, Warner Bros. Discovery, Bristol Myers Squibb, Amgen, Evernorth, State Street, Mastercard, Hitachi, Medtronic, S&P Global, Silicon Labs, Analog Devices, Marvell, Thermo Fisher, Cisco |
| Failed (other errors) | 0 | — |

All 26 Workday API companies have had no successful discovery since 16 Nov
06:04–06:21 UTC. The dashboard shows them as failing (Critical, 26/44).
That is accurate, and they stay in the failure count until a real
successful scan.

## Per-data-centre recovery (PRs 24–27, 19 Nov)

The canary rollout above had one weakness. A single global canary (Sanofi and
PwC, both on wd3) held all 24 other Workday companies, across four
independent Workday data centres. A 429 on wd3 said nothing about wd1, wd5
or wd12, yet it blocked them all. Each further 429 also pushed the next try
3 → 6 → 12 h out.

### What changed

| PR | Merged | What it does |
|---|---|---|
| [#24](https://github.com/manojanumolu/job-tracker/pull/24) (A) | `a68a057` | Gating is per data centre (`wdN` in the host), not global. A DC is gated while its newest API 429 is newer than its newest real healthy scan. Each gated DC gets exactly one probe once its throttled hosts' cooldowns have passed; canaries are preferred in config order. Probes run first, and a healthy probe releases only its own DC, within the same run. A 429 closes that DC for the rest of the run (`cluster_gate`). Allow-listed response headers (`retry-after`, `ratelimit*`, `x-ratelimit-*`, `server`, `via`) are stored on throttled hosts. |
| [#25](https://github.com/manojanumolu/job-tracker/pull/25) (B) | `8a94da1` | `check_jobs.yml` takes `scope: all \| workday`. A Workday-only run scans only the Workday API companies, never consumes the 3 h full-scan window (`last_scan` counts only `scope == all` runs), and keeps every other company's data untouched. |
| [#26](https://github.com/manojanumolu/job-tracker/pull/26) (C) | `21b55e7` | The Scan scheduler chain dispatches a Workday-only plan run 2 min after a gated DC's cooldown expires, when that is at least 20 min before the next full window. Each expiry earns at most one targeted run. |
| [#27](https://github.com/manojanumolu/job-tracker/pull/27) (D) | `6d6b92f` | Fixes two issues seen live. (1) While a dispatched recovery run had not yet published, each new chain run saw the recovery as still due and dispatched again: 7 Check Jobs runs at 07:41–07:43. Only one scanned (2 Workday requests); the rest were cancelled by the `check-jobs` group or planned no scan. The chain now waits 15 min instead. (2) A DC whose members the run's own two-host safety stop or request budget deferred (wd5 at 07:09) stays due for its probe. |

Unchanged: the two-throttled-hosts safety stop, the 600-request cap, 2 s
global and per-host spacing, 3/6/12/24 h cooldown escalation, the 72 h
verdict cache, and the 6 h revisit. No Retry-After was ever received, so
none could be bypassed.

CI, full suite: PR A 3,659 passed / 0 failed / 39 skipped; B + C 3,673 / 0 /
39; D 3,675 / 0 / 39. All skips are Firestore-emulator tests.

### Production results (real Workday, GitHub Actions)

| Time (UTC) | Run | DC probed | Result |
|---|---|---|---|
| 01:30 | [Check Jobs 1529](https://github.com/manojanumolu/job-tracker/actions/runs/44437522154) (old global canary) | wd3 Sanofi, PwC | 429, 429 |
| 07:01 | [Check Jobs 1534](https://github.com/manojanumolu/job-tracker/actions/runs/44476337123) (PR A, full scan) | wd12 Salesforce, wd1 Micron | 429 `server: cloudflare`, 429 `server: cloudflare`. wd5 was then held by the two-host safety stop; wd3 was still cooling. |
| 07:42 | [Check Jobs 1536](https://github.com/manojanumolu/job-tracker/actions/runs/44481571798) (PR C auto-dispatch, `scope=workday`) | wd3 Sanofi, wd5 NVIDIA | 429 `server: cloudflare`, 429 `server: cloudflare`. 2 requests in total. |

Every Workday CXS data centre the tracker uses (wd1, wd3, wd5, wd12) has now
answered its single probe with HTTP 429. No 429 carried `Retry-After` or any
`RateLimit` header; every one carried `server: cloudflare`. Requests were
spaced at least 2 s apart, one per DC, after cooldowns of 3 h or more. This
is not a per-tenant request counter. It is an edge (Cloudflare) rule applied
to this client, the GitHub Actions runners, and it has been in force since
16 Nov 09:04 UTC. JioStar (wd102), read from its public sitemap rather than
the CXS API, is healthy.

### Company status after the 07:42 run (data commit `4f74f45`)

| State | Count | Companies |
|---|---:|---|
| Healthy (real successful scan) | 18 | 17 non-Workday portals + JioStar (sitemap) |
| Recovered Workday API companies | **0** | — |
| HTTP 429 this run | 2 | Sanofi (wd3, cooling to 19:42, failure 3), NVIDIA (wd5, cooling to 13:42, failure 2) |
| Cooling down after a 429 at 07:09 | 2 | Salesforce (wd12, to 13:09), Micron (wd1, to 13:09) |
| Deferred: waiting for their DC's probe | 22 | wd1: Wells Fargo, Invesco, Amgen, State Street, Mastercard, Hitachi, Medtronic, Silicon Labs, Analog Devices, Marvell; wd3: PokerStars, PwC, Novartis; wd5: Carrier, Synchrony, Broadridge, Warner Bros. Discovery, Bristol Myers Squibb, Evernorth, S&P Global, Thermo Fisher, Cisco |

No company was marked healthy by being released from a gate. All 26
Workday API companies remain in the dashboard's failure count until a real
successful scan.

The next probes happen automatically: wd1 and wd12 after 13:09, wd5 after
13:42 (targeted runs, if 20 min clear of a full window, otherwise inside the
full scan), and wd3 after 19:42. Each costs one request per DC. A further
429 escalates that DC's cooldown, capped at 24 h, so the request load while
blocked stays at most 4 requests per cooldown period.

### Remaining blocker and permitted options

The Cloudflare block on the Workday CXS API for this client is outside the
tracker's control. Changing IP, user agent or headers to get around it
would be evasion and is not done. The permitted options are operator
decisions:

1. Keep the automatic per-DC probes running. They are cheap and will detect
   a lift within one cooldown.
2. Ask Workday or the employers for permitted API access for this client.
3. Per company, verify an officially offered alternative feed (as with
   JioStar's sitemap) before adding it.

## Remaining blocked companies and operational limitations

* **The 26 Workday API companies are still not being monitored
  successfully.** Workday continues to return HTTP 429 to the canary. This
  is an external restriction that the tracker respects; it is not bypassed.
  If it persists past the 24 h cooldown ceiling, the remaining permitted
  options are an operator decision: ask the employers or Workday about API
  access, or verify an officially permitted alternative feed per company.
  Neither is automated here.
* The Workday limit is external and undocumented. This change makes the
  tracker send ~88% fewer requests, never retry a 429 blind, and stop on
  repeated throttling. It cannot force Workday to lift a limit. If the canary
  keeps getting 429, the 24 others stay deferred (visibly) and the canary
  cooldown escalates 3 → 6 → 12 → 24 h.
* Sanofi and PwC have no verified permitted alternative feed. No sitemap or
  feed endpoints were invented or activated; JioStar's existing sitemap is
  unchanged.
* Changes to a posting's detail page that don't change its title, location,
  URL or card text are picked up on the 72 h revalidation, not instantly.
* GitHub cron is best-effort; the self-dispatching Scan scheduler chain
  (unchanged) is what keeps the 3 h windows on time.

## Verification record

* Root cause: measured from data branch history (per-company scan counters
  for every scan since 26 Oct) and the Actions run list. See the table at
  the top.
* Code: CI full suite green on both merged heads (`3ff3987`: 3,647 / 0 / 39;
  `52370f7`: 3,653 / 0 / 39). The local full suite matched on `52370f7`. Two
  earlier PR 22 CI runs failed (4 dashboard tests) and were fixed before
  merge; see defect 3.
* Production behaviour: data commits `c4bda05` (first M7 run), `2559299`
  (00:02), `833ea57` (00:14) and `d17aae1` (01:30 canary). Each was read
  back from `main` and checked for request counts, outcomes, cooldowns,
  generation match and alert-ledger stability.
* Not verified, because it cannot be until Workday lifts the limit: a
  successful canary scan, automatic promotion to full rotation, the cold
  sweep under the 600-request cap, and steady-state request volume. These
  are covered by tests (`test_auto_mode_promotes_after_canary_success_and_demotes_on_new_429`,
  `test_rotation_covers_26_every_6_hours_with_half_per_window`,
  `test_second_window_within_revisit_makes_no_requests_and_stays_healthy`)
  but not observed live.
