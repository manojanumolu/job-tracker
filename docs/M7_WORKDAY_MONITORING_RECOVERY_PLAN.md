# M7 — Workday Monitoring Recovery Plan

Status: **planning complete; implementation and live probes not approved**.

Prepared on 18 November 2026 from local commit `8e33074d2d5a6cb7b2054c10b9b39aadda2aa04f`, on documentation branch `feature/m7-workday-recovery-plan`. All repository paths below are relative to `company/`. Proposed filenames are explicitly identified. No live endpoint, robots.txt, GitHub Actions run, repository setting or current remote state was queried. Historical documentation is evidence of earlier observations, not present-day access permission.

## 1. Executive summary

The committed snapshot confirms **44 companies, 18 active and 26 failing (59.1%)**, and 142 seen-job records. Every failing company records HTTP 429 from a Workday API. There are **26 Workday API companies**, including PwC with two configured sites, plus **one healthy Workday sitemap company, JioStar**. Thus there are 27 Workday-hosted companies but 26 failing API companies; these counts must not be interchanged.

The code confirms amplification mechanisms: unpaced Workday pagination and detail requests, no Workday Retry-After handling, no persistent cooldown, and repeated evaluation before deduplication. It does **not** establish the provider's rate-limit scope or prove an IP ban. A shared Workday limit, runner-IP restriction, endpoint-specific policy, or tenant policy remain hypotheses.

Recommended sequence: approved bounded diagnostics; truthful health reporting; Workday-specific request control and durable cooldown; a separate verdict cache; then company rotation with scheduler changes; finally two evidence-qualified alternative-source pilots. Keep the 18 currently active integrations operating, including JioStar. Do not change eligibility, recipient selection, alert retry semantics, application tracking or M6.

Nine API companies per three-hour window gives `ceil(26/9) × 3 = 9 hours` nominal revisit time. It is a capacity target, not a recovery guarantee. Cooldowns, unavailable sources, workflow delays and request budgets can prevent it. The 24-hour safeguard must surface overdue coverage and prioritize safe attempts, never override a site's cooldown or access restriction.

## 2. Verified incident evidence

### Snapshot and provenance

| Evidence | Locally verified result |
|---|---|
| Initial working tree / branch | Clean / `main`; requested documentation branch did not exist and was created |
| Tracked files | 170 |
| M6 merge | `af28348`, PR #20; M6 source commit `f9b1d77` appears in source history |
| Company statuses | 18 `active`, 26 `failing`; no other status in this snapshot |
| Sources | 26 Workday API companies; 1 Workday sitemap; 17 other companies |
| Saved jobs | 142 records in `data/seen_jobs.json` |
| Configured interval | `data/settings.json`: `scan_interval_hours = 3` |
| Latest saved run | 2026-11-18 12:03:16–12:10:17 UTC (17:33:16–17:40:17 IST) |
| Run summary | 44 portals, 26 failed, 79 eligible jobs found including known jobs, 0 new jobs, 44 reposts suppressed |
| Failure streaks | Salesforce: 21; other 25 failed companies: 18 each |
| Last successful API checks | Salesforce: 15 Nov 21:07 UTC; other 25: 16 Nov 06:04–06:21 UTC |

The latest failed run lasted about seven minutes, not the workflow comment's historical 20–26 minutes. Early API failures can make a broken scan look fast. Zero new jobs is not evidence of complete discovery. Counts do not represent a live check on the date this document is later read.

**Failing (all Workday API / recorded HTTP 429):** PokerStars, Sanofi, PwC, Salesforce, Micron, NVIDIA, Wells Fargo, Novartis, Carrier, Synchrony, Broadridge, Invesco, Warner Bros. Discovery, Bristol Myers Squibb, Amgen, Evernorth (Cigna), State Street, Mastercard, Hitachi, Medtronic, S&P Global, Silicon Labs, Analog Devices, Marvell, Thermo Fisher Scientific, Cisco.

**Active in the saved snapshot:** MetLife, Avalara, Accenture, NPCI, Amazon, Microsoft, Google, Infosys, Deloitte, Qualcomm, JioHotstar (JioStar), AMD, Netflix, Oracle, IBM, ADP, ServiceNow, Swiggy.

Examples tie the finding to actual fields: `pokerstars.status_reason` names `flutterbe.wd3.myworkdayjobs.com`; PwC records two 429 errors from `pwc.wd3.myworkdayjobs.com`; Salesforce names `salesforce.wd12.myworkdayjobs.com`. `last_checked` is an attempt timestamp, while `last_success` remains older on failures. Existing records do not retain Retry-After or distinguish the failed request phase structurally.

Source history inspected includes `a68c526` (catalogue/source expansion), `b31d1d6` (Eightfold retry), `87f80f1` (failure streaks/feed guard), `588eb08` (JioStar sitemap), `91f364c` (discovery fixes), `267239b` / `08912ba` (repost window). A full historical request-volume reconstruction was not performed; “approximately three to 25–27 companies” is prior context, not a measured traffic series.

## 3. Actual root-cause investigation

| Finding | Confidence / implication |
|---|---|
| Saved Workday API requests received 429 | Confirmed by company records; server signalled rate limiting, but scope/cause unknown |
| Workday list pagination has no spacing/backoff | Confirmed in `sources.workday_listings` and `_get_json` |
| Workday detail calls bypass `_get_json` | Confirmed in `workday_detail`; non-200 becomes `Detail.unreadable` |
| Detail 429 does not stop subsequent details | Confirmed by `_evaluate` / `_scan_sources`; requests can continue until budget exhausted |
| Known accepted/rejected postings are reevaluated | Confirmed: seen-job merge happens after scanning; no persistent verdict lookup |
| All configured companies retried on subsequent runs | Confirmed: `run_all` has no cooldown or rotation |
| Requests are simultaneous within current scanner | Not supported: loops are synchronous; bursts are sequential requests with little spacing |
| Shared provider / egress-IP restriction | Plausible across multiple `wd1`, `wd3`, `wd5`, `wd12` hosts, unconfirmed |
| A universal Workday-host outage | Not supported by active JioStar sitemap; API-specific restriction remains possible |
| Pacing alone will restore all 26 | Unproven; permitted alternative sources or provider cooperation may be needed |

The common failure timing strengthens the shared-limit hypothesis but cannot separate it from endpoint policy changes. Historical response headers, request counts and egress evidence are absent. Do not diagnose an IP ban from 429 alone. No proxy rotation, identity spoofing, CAPTCHA workaround, credentialed endpoint or browser fallback is proposed.

## 4. Current architecture and exact integration points

1. `.github/workflows/check_jobs.yml` plans on cron `17,47 * * * *`; a separate `20 */6 * * *` pass delivers personal alerts. Manual mode `scan` forces a scan; mode `plan` checks whether due. `.github/workflows/scan_scheduler.yml` maintains the dispatch chain and retries due windows, with cron as a safety net. These schedules remain unchanged in this planning task.
2. `scan_schedule.plan`, `last_scan`, `latest_run`, `chain_wait` read settings and company data. **`last_scan` currently returns the minimum `last_checked`**, assuming every company gets checked each run. A rotation cannot retain this assumption. `latest_run` reconstructs a completed run from `company.scan.run`.
3. The scrape job checks code and data out separately, uses `contents: read`, `persist-credentials: false`, no secrets, and `JT_DATA_ROOT=jt-data`. It runs `src/scraper.py`; the job timeout is 90 minutes.
4. `scraper.run_all` loads companies and seen jobs via `config_store`, excludes companies with a nonempty `monitoring_state`, then calls `scan_company` sequentially. `sources_for` uses `COMPANY_SOURCES` overrides or infers Workday configuration from a URL. Configured source failures do not justify switching to browser scraping.
5. `_scan_sources` creates a new `httpx.Client` per company (`timeout=25`, redirects enabled), reads source listings, then `_evaluate` performs the title prefilter, fetches detail, and calls `decide`. `decide` runs the classifier and eight safety checks, then attaches evidence, skill and M6 requirement information. `MAX_API_DETAIL_FETCHES=500` is per company. Excess detail demand causes random shuffling and visible pending counts.
6. `workday_listings` starts with an unfiltered facet request, optionally repeats page zero with India facets, then paginates in pages of 20 up to 2,000 postings. Duplicate external paths are removed within that site scan. There is no explicit truncation/completeness signal when the cap is reached. `workday_detail` fetches each remaining candidate directly. `run_all` sleeps one second **between companies**, not between Workday requests.
7. `Scan.status` reports failed list sources, but detail failure becomes `failing` only after at least three attempts with more than half unreadable. One or two unreadable details, or a smaller failure fraction, may leave `active`. Pending work can also coexist with `active`. M7 should add completeness diagnostics without pretending every old active result was exhaustive.
8. `merge_new_jobs` / `identity.job_uid` deduplicate after discovery. Repost suppression uses the title/location key and 45-day last-sighting window. Known accepted jobs must still contribute sightings; a suppressed repost must not extend that window. `seen_jobs.json` is an accepted-job and delivery ledger, not a cache of all evaluated postings.
9. The `scan-results` artifact carries **only `companies.json` and `seen_jobs.json`**, retained one day. `scan_handoff.take_over` checks an allowlist, file type, UTF-8 JSON shape and 20 MiB size, validating all before copying. `logos.json` is allowed for the separate logo workflow, not currently included in scan-results. Validation is basic shape validation, not full semantic trust validation; copying is not crash-atomic across files.
10. The publish job runs trusted code in the `production` environment with write/email credentials. `alerts.run` initially publishes both files and claims, then publishes only seen jobs for sending/finalization. `repo_sync.publish` fetches latest data, merges, resets **the separate data checkout**, commits and pushes with retries. Unknown filenames currently fall through to wholesale replacement: new state requires explicit merge handlers.
11. `repo_sync.merge_record` preserves delivery claims, terminal states, dismissal and monotonic sighting stamps. `merge_companies` preserves remote membership/configuration but unconditionally overlays local scan fields; it does not reject stale scan results. Workflow concurrency `check-jobs` reduces scan races but does not serialize app edits, logos or every other writer.
12. `user_alerts.select_jobs` / `run_for_user` use personal preferences, verified account addresses, six-hour cadence and Firestore delivery claims. Shared recipient and tailoring are independent. `streamlit_app.portal_status`, `_health`, `page_monitoring`, `tracker_status_card`, `scan_details_rows` display health with account scopes. Existing UI already flags failing portals; the gap is explicit rate-limit/deferred/cooldown categories and severity/freshness aggregation. `_health` can reach “All portals healthy” without requiring every individual status to be healthy when other aggregate checks pass.

`src/discovery.py` organizes tailored/beyond-preferences results; it is not the HTTP discovery engine. `src/snapshot_cache.py` is the app's shared snapshot freshness cache, not a posting verdict cache. Do not repurpose either as Workday storage.

## 5. Proposed architecture and eight recovery priorities

All names in this section for new modules/files are proposals. All numeric tuning values are recommendations awaiting approval, not production defaults established by this document.

### Priority 1 — Respectful request pacing

Add a Workday request policy in proposed `src/workday_http.py`. A run-scoped controller is injected across company clients: recreating `httpx.Client` must not reset global or host pacing. Apply it to API list/detail and Workday-hosted sitemap/page traffic, including permitted redirects. Other adapters retain their existing behavior.

Use monotonic time for in-process spacing, normalized hostname for per-host limits, and a separate Workday-wide next-request time. Permit only one in-flight Workday request. Earliest request time is the maximum of global spacing, host spacing and persisted cooldown. No token-bucket burst allowance. Add a configurable inter-company delay; do not mistake this for request pacing. Use UTC only for persisted deadlines, and injectable clock/sleeper/randomness in tests.

Candidate pilot values: global spacing 1 second, host spacing 2 seconds, inter-company delay 10 seconds; tighten to any published restriction. These are conservative starting experiments, not claims of provider-approved rates. At 500 details × 2 seconds × 9 companies, spacing alone costs 150 minutes before lists or network time: **500 is a safety ceiling, not an executable per-company target** within a 90-minute workflow. Introduce a Workday request/time budget with a proposed 30-minute Workday allocation and a cooperative whole-run deadline leaving at least 10 minutes before the workflow timeout. Measure non-Workday duration before enabling these values. Stop and persist pending work before the deadline; never shorten pacing to fit.

Keep the identifying tracker user agent honest; the existing header includes `FresherJobTracker/1.0`. Review a clear product/contact user agent during approved policy assessment. Do not randomize headers or imitate browser navigation. Access policy takes priority over throughput goals.

### Priority 2 — HTTP 429, bounded backoff and cooldown

Introduce typed HTTP diagnostics on `SourceError` and unreadable `Detail` (or an equivalent shared result), with optional fields so other adapters remain compatible. Distinguish rate limit, forbidden/policy block, authentication, not found, timeout, network, server error, malformed data and identity mismatch. Preserve request phase (`list`, `detail`, `sitemap`, `job_page`). Avoid parsing exception strings for new behavior.

Parse Retry-After delta seconds and HTTP-date; invalid/negative/past values use bounded fallback. Delay is at least a valid server deadline and exponential fallback, with only nonnegative jitter. Proposed fallback: 30 seconds then 60 seconds, at most two retries (three total attempts), and no more than 120 seconds of in-run retry waiting. A valid longer Retry-After immediately becomes a persisted cooldown; never cap a valid server deadline downward. Extremely distant dates should stop requests and raise an admin diagnostic rather than silently retry early.

On the **first 429**, open the host circuit immediately: queued requests, other PwC sites and detail iteration must not continue. The bounded retry policy permits only a single designated half-open trial after waiting; repeated 429 closes the host for the remainder of the run. Detail 429 is not cached as rejection. Preserve successfully evaluated jobs but mark the company rate-limited/incomplete. Resume pending jobs later.

Proposed persistent fallback cooldown: 3 hours initially, doubling across failed trial scans to 6/12/24 hours; server Retry-After can exceed this fallback cap. Retry state is host-wide with per-company/source diagnostics. Expiry permits one trial, not a flood; success closes the circuit only when its observation is newer than the failure. Ordinary deferred scans neither increment failure streaks nor erase them. A later complete company scan resets company failure streaks.

Consider a Workday-wide breaker after first-request 429 on three distinct hosts in one run, to avoid contacting all 26 when a common restriction is likely. This is an optional approval decision: it protects the service but could unnecessarily pause JioStar. Initially scope the aggregate breaker to API traffic while retaining global spacing and host circuits for sitemap traffic. A 403/robots denial is an access-policy failure, not something to retry or bypass. Bounded network/5xx retry, if added, must remain distinct and must not change other adapters' policies accidentally.

### Priority 3 — Persistent verdict cache without silent job loss

Use proposed `data/workday_verdict_cache.json` through `src/workday_cache.py`, separate from companies and seen jobs. Key by immutable company ID, source/tenant/site namespace, and ATS ID or canonical posting path; retain the existing `job_uid` mapping for alert identity. Workday API `Listing.job_id` is currently not populated, so use its external path/URL identity until an authoritative ID is available. Avoid display-name-only cache keys and prevent cross-tenant ID collisions.

Each entry should include schema version, source configuration fingerprint, listing fingerprint, evaluation timestamp, expiry, eligibility/extraction version, outcome/reason code, detail content hash, and bounded accepted-job evidence when applicable. Listing fingerprint covers title, location, path and available modification metadata. Do not hash relative “posted N days ago” text as if it were a reliable revision. Detail hash detects changes **after fetching**; it cannot establish that an unfetched detail stayed unchanged. Require periodic revalidation even if listing fields match.

Recommended initial TTL is 24 hours for definitive accepted/rejected decisions, pending evidence from Stage 0; shorter TTL improves changed-detail discovery at more request cost. New or changed listing fingerprints, source changes and classifier/safety-gate/M6 extraction version changes invalidate immediately. No durable negative verdict for unreadable, throttled, budget-deferred or malformed details. Track them as pending work with next-eligible time, not permanent rejection. Expiry uses evaluation time, never extends on cache hits. Garbage collection may remove old cache entries, never seen jobs or delivery records.

Order detail work: new and visibly changed postings first; then overdue pending/expired entries using an age-based fair queue with a reserved share so new arrivals cannot starve revalidation. Persist pending identity, reason and first-deferred time; expose pending counts and oldest age. A candidate share is 75% new/changed and 25% oldest pending, borrowing unused capacity; this needs approval and simulation.

On a valid accepted cache hit, require the posting to appear in the current successfully obtained listing and restore the full validated job/evidence payload for `merge_new_jobs`. Preserve `posted_at`, skills and M6 requirements. Mark the result as reused evidence with its evaluation age in diagnostics; do not falsely report a fresh detail fetch. Existing first_seen and ledger fields remain authoritative. On a negative hit, increment the same rejection counters/reasons. On expiry with no budget, leave pending and do not stamp a fresh qualifying sighting. A failed or unattempted listing cannot revive cached jobs as newly discovered.

Never copy cache entries straight into the delivery ledger. Cache is an optimization; classifier safety gates, canonical identity, existing repost suppression and shared/personal selection remain authoritative. Rule-version mismatch requires fresh evaluation; if retaining sufficient sanitized evidence for offline reevaluation would enlarge public storage, prefer refetch. For already-known changed postings, preserve current no-realert behavior; changing how old displayed records are refreshed is a separate scope decision.

Measure uncached-vs-cached output equality on fixtures, cache hit rate, list/detail request counts, expired/pending age, version misses and preserved sightings. A warm repeat of identical fixture listings should make zero detail calls for valid hits while producing identical eligible identities and downstream ledger decisions. Real reduction targets must be based on observed inventory, not the 142 accepted records alone.

### Priority 4 — Controlled rotation and freshness

Recommend rotating the **26 API companies** in batches of up to nine while keeping JioStar and the other 17 active integrations on the existing cadence. Including all 27 Workday companies also gives three batches, but needlessly reduces JioStar freshness; decide explicitly before rollout. PwC is one company slot but two source sites sharing a host budget. A slot count alone does not bound HTTP requests.

Filter prohibited/cooling hosts first; select eligible never-attempted and overdue companies, then oldest actual attempt with a stable company-ID tie-break. Record attempted work separately from successful freshness. A failed eligible attempt advances attempt fairness and starts cooldown; it must not reset last_success. Otherwise permanently failing companies sorted only by oldest success would monopolize every batch. Fill vacant slots from eligible companies, within request/time budgets. Source configuration changes invalidate stale scheduling assumptions; removed companies do not remain in the queue.

Nominal round-robin revisit time is `ceil(N/B) × interval`; changes to the admin interval or catalogue size change the estimate. At a six-hour interval with 26/9 the target becomes 18 hours, so do not promise nine hours unconditionally. Report both last-attempt age and last-success age. First failed attempts may be timely while recovery remains overdue.

The 24-hour safeguard promotes safe eligible attempts over ordinary rotation, reports companies with no success in 24 hours, and alerts on persistent coverage loss. It does not reset cooldown or force nine successful companies. If more overdue companies exist than capacity, show backlog and unmet target; any temporary batch increase requires a separately approved policy.

Before enabling **any skip**, including Stage 2 cooldown, replace scheduler inference from minimum company timestamps with a durable completed-run marker. Recommended source is proposed `workday_scan_state.json`'s run envelope, updated for every full orchestration window even if all Workday companies cool down. `plan`, scheduler wait, UI run times and fallback behavior must consume the same marker. Preserve individual timestamps. On legacy state use a validated `scan.run` record, then documented legacy fallback; missing/corrupt new state raises a warning and must not cause a rapid catch-up loop. Manual “Run check” may bypass cadence, never host cooldown/policy controls.

### Priority 5 — Official alternative discovery

Repository evidence: `sources.workday_sitemap_listings` reads `{site}/siteMap.xml` and `workday_sitemap_detail` validates schema.org JobPosting identity and description. Existing JioStar tests verify its old job identity survives the source switch. `docs/MONITORING.md` reports the 9 November sitemap had 100 jobs versus about 230 previously in the API. This is historical coverage evidence, not proof that sitemap absence means a job closed. The adapter does not itself refresh robots permission on every call.

Also inspect existing `job_feed_listings` patterns for ADP and ServiceNow; their existence does not imply a corresponding feed for any Workday tenant. Discovery must follow employer-published career links, robots Sitemap directives, or documented public feeds. Do not invent endpoints or infer permission from a 200 response alone. robots permission is not a replacement for terms/access-policy review.

After Stage 0 approval, build a per-company matrix: official origin, exact published discovery link, robots rules for list and detail paths, applicable terms, discovery time, response status, pagination/index support, identity quality and observed coverage. Unknown or denied access stays blocked/pending review. Validate sitemap indexes, namespaces, encodings, bounded document sizes and page counts; reject external entities, unexpected hosts, private/local destinations and redirect escapes. Keep official allowed destinations explicit and prevent arbitrary URLs in cache from becoming fetch instructions.

Stage 5 must pilot **two failing companies with positively verified permitted alternatives**. Tentative assessment candidates are Sanofi (explicit non-Workday public career URL in existing configuration) and PwC (two sites needing coverage validation); neither is declared suitable yet. Replace either if policy or coverage evidence fails. JioStar is a regression control, not one of the two recovered-company pilots. If fewer than two qualify, stop that stage and report why. Never fall back automatically from a denied API to an unapproved channel.

### Priority 6 — Structured diagnostics

Proposed `src/monitoring_health.py` provides pure aggregation; typed source diagnostics originate at the request layer. Per-company/source observations include:

| Field group | Required content |
|---|---|
| Identity | Immutable company ID, display name, discovery method, source key, normalized host |
| Attempt | Run ID, attempted_at, finished_at, request phase, numeric HTTP status or null |
| Outcome | Outcome enum, failure category, safe reason code, complete/partial flag |
| Retry | Parsed Retry-After seconds/deadline and validity indicator, retry count, cooldown_until, cooldown reason |
| Freshness | Last actual attempt, last complete success, first failure, consecutive attempted failures |
| Coverage | List/detail requests, candidates, accepted/rejected/unknown, cache hits/misses, pending count/oldest age, truncation |
| Scheduling | Selected/deferred/cooling state, next eligible time, last scheduling decision; kept distinct from attempt |

Represent no HTTP request with null status, never invented HTTP 200. Multi-source companies retain per-source outcomes rather than only the final exception. Partial failure cannot advance complete-success time. Existing diagnostics and ADP feed baselines remain intact.

Only allowlisted typed values enter public JSON/logs: do not store cookies, Authorization, Set-Cookie, raw response bodies, tokens, credential URLs, query strings or arbitrary headers. Store parsed Retry-After, not unrestricted header dumps. Bound strings, escape control characters/Markdown/HTML, and sanitize Actions annotations against workflow-command injection. Names and source text are untrusted. Routine logs use counts and reason codes; private accounts, recipient addresses, resumes and Firestore IDs never belong here.

### Priority 7 — Monitoring health warnings

Use two dimensions: last observed outcome and current scheduling state. Render six requested categories without losing their relationship: **healthy, failed, rate limited, deferred, cooling down, not yet checked**. Example: “Cooling down; last attempt rate limited; last success 52h ago.” A deferred previously healthy company may be within its rotation target, but the current window did not check it. Deferred previously failed companies still contribute failure severity.

For each explicitly labelled scope, let N be all configured monitored companies, and F the union of companies with unresolved failed/rate-limited outcomes, including cooling/deferred ones. Count each company once, not each PwC site. Critical if `N > 0 and F/N >= 0.50`; otherwise degraded if `F/N >= 0.25 or F >= 5`. Show pending/unknown/stale/partial counts independently; do not shrink the denominator to attempted companies or silently exclude cooldowns. Zero-company scope means “No portals configured,” never 100% healthy. Use comparisons before rounding.

A separate coverage warning is required when never-checked, stale or partial coverage is substantial, even if F=0. Recommend applying the same 25%/5 and 50% bands to the union of unavailable/unknown/overdue companies, displaying “coverage degraded/critical” separately from measured failures. In-target rotation deferrals are not failures. Complete health is allowed only with recent trustworthy success for all companies, no unresolved failures/unknown coverage, and fresh snapshot/run data; otherwise state the qualification explicitly.

The snapshot is critical at 26/44. Preserve per-admin scope: `MONITORED` and primary shared-alert scope must not be replaced by an unrestricted personal catalogue. Add a clearly labelled operational whole-catalogue aggregate only for authorized admins; it is not a change to their alert preferences.

Actions: emit safe `$GITHUB_STEP_SUMMARY` tables and warning annotations in the scrape job, then show whether data actually published. A successful Python/workflow exit currently does not imply healthy portals. Recommend warnings initially, with an optional separate final health-check job that fails on critical coverage **after publication and existing alerts**; never fail the scraper merely to color the run red and thereby suppress its publish dependency. Infrastructure failure, missing artifact and stale last-published run must be visible even when no fresh health report exists. No new health email/Slack notifications without approval.

### Priority 8 — Preserve the application and alerts

Keep shared `alerts.claim` / `run` / `finalize`, personal `run_for_user`, admin preference scopes and recipient checks semantically unchanged. Only extend the initial data publication payload where required. Later sending/finalization publishes remain ledger-only, preventing a stale copy of cooldown/cache data from replacing newer state.

Required invariants: no email before durable claim/sending publication; failed publication sends nothing; claim ownership compare-and-swap survives races; uncertain delivery is never retried; definitive failure retains existing bounded retries; dismissed/terminal states never regress. No recipient data enters public artifacts. Source/cache recovery never clears `first_seen`, `notified`, claim IDs, delivery timestamps or personal ledger documents.

Preserve canonical identities across API/sitemap methods, the 45-day repost window and its deployment backfill, current shared cutoff and personal activation cutoff, shared tailoring and per-user filtering. Cache acceptance never overrides `passes_global_gate` or the existing eight evidence checks. No weaker seniority, experience, India-location, internship or freshness rules. Keep M6 evidence/requirements, application document identities, saved resumes and UI navigation intact. Healthy non-Workday adapters retain their timeout, Eightfold retry, Deloitte retry and ADP feed-anomaly behavior.

## 6. Data persistence, security and concurrency design

### Proposed files and access

| File | Purpose / reader and writer |
|---|---|
| Existing `data/companies.json` | Catalogue/configuration plus compact latest monitoring display projection; app and scanner/publisher retain existing roles |
| Existing `data/seen_jobs.json` | Accepted jobs, deduplication, shared ledger; no cache semantics |
| New `data/workday_scan_state.json` | Versioned host cooldown observations, company attempt/rotation state, pending references and completed run/window marker; read by scan, plan/scheduler and health views; written through trusted publisher |
| New `data/workday_verdict_cache.json` | Versioned bounded public posting verdict/evidence cache; read by scrape, validated and persisted by publisher; not needed by personal alerts or scheduler |

Host cooldown and scheduling belong in separate state, not embedded cache entries in companies.json. Company records may project display fields under `scan` but are not the authoritative cooldown store. Use an envelope with schema version, generation/base revision, run ID, started/finished UTC times and observations. Proposed example shape (illustrative, not a production schema):

```json
{
  "schema_version": 1,
  "generation": 7,
  "base_revision": "<data-commit>",
  "last_completed_run": {"run_id": "<id>", "window_start": "<UTC>", "finished_at": "<UTC>"},
  "hosts": {},
  "companies": {},
  "pending": {}
}
```

### Handoff and durability

Recommend the existing artifact transport plus explicit git data-branch persistence. Artifact retention of one day is suitable for same-run transport, not a durable cache. Actions cache is evictable and unsuitable as the authoritative cooldown/rotation store. A private database adds credentials to the design and is unnecessary for this public-data optimization. A dedicated configured data branch is architecturally supported; actual `JT_DATA_BRANCH`/environment rules are unverified and must be reviewed before rollout.

Extend the scrape artifact and trusted `scan_handoff.SHAPES`/`_check` allowlist to exactly four files at Stage 3 (three at Stage 2). Extend the first `alerts.run` publication to load all required new files **before** `repo_sync.publish` resets the data checkout. Otherwise the new cache could be discarded before it is committed. Add explicit merge dispatch, do not rely on the unknown-file replacement branch. The same commit must contain company projection, seen jobs and the corresponding new state/cache generation. No code, pickle, serialized callables or environment files cross the boundary.

Plan/scheduler sparse checkouts must add state at Stage 2 and pass `JT_DATA_ROOT`. The app needs a bounded snapshot reader for state or a coherent derived health projection; avoid introducing a second unsynchronized cache. Personal alerts and logos do not need new cache content. Ensure unrelated sparse checkouts and source-code imports remain unchanged.

Validate all files before adoption: supported schema, unique keys, types/enums, bounded nesting/count/size, timestamp ranges, configured company/source/host membership and safe URLs. Reject symlinks, unsupported filenames, path traversal and oversized input. Use staged writes and a generation manifest/check before use; validation-before-copy alone does not ensure multi-file crash atomicity. Git publication supplies the durable atomic commit. A checksum detects accidental mismatch but does not make untrusted scraper content trusted.

### Missing/corrupt state

* First deployment with no verdict cache: explicit cold-cache mode, pacing and budgets remain on; warn and rebuild gradually. Do not convert missing cache to negative results.
* Bad verdict entry from the durable store: reject the entry, record a bounded corruption count and refetch safely; incompatible whole schema becomes a visible cold-cache rebuild. Do not overwrite a newer unsupported format without migration approval.
* Missing cooldown state during explicitly versioned first deployment: bootstrap conservatively from recorded 429 evidence and approval-selected cooldown policy. Unexpected absence/corruption after deployment: fail closed for Workday requests, report state unavailable, continue healthy unrelated scanners where possible. Do not silently reset cooldowns.
* Missing required or malformed new artifact after the producing feature is enabled: fail handoff before publishing/sending, matching the existing security boundary. Last good remote data remains authoritative; independent scheduled personal delivery behavior is unchanged. A producer wishing to rebuild an optional cache must emit a valid empty, explicitly flagged envelope, not omit required files.
* Persisting state fails: never claim recovery/durability or send shared mail. Because unpersisted cooldown can be lost on runner death, preflight-load safety and conservative failure handling remain essential. A separate partial-progress artifact protocol would be additional work; do not upload partially written files with an unconditional `always()` step.

### Concurrent writes

Keep workflow concurrency and non-cancelling behavior, but rely on compare-and-swap publication against the latest fetched data tip. Merge only observations generated by this run, not an entire stale schedule snapshot. Company membership/configuration remains remote-authoritative; discard results when source configuration changed since scan start. Add run/attempt ordering to scan-field merges so older attempts cannot replace newer results.

For host state, newer failures extend cooldown; stale successes cannot clear a later failure. Only a success from a trial after the applicable failure may close it. Use observation timestamps plus run/generation ordering to resolve ties, not a blind maximum cooldown forever. Cache entries merge by valid schema/rule/source version and newest evaluation, preserving unrelated entries. Deletions/expiry must be conditional on the entry version observed so stale pruning cannot delete a concurrent refresh. Completed-run marker advances monotonically; queue selection advances only for actual attempts. Test push rejection/retry against app edits, company removal and another run. Never change delivery merge precedence to implement these rules.

## 7. Affected-file inventory

Existing files inspected and intended touchpoints:

| Existing path | Functions / planned responsibility |
|---|---|
| `src/sources.py` | `SourceError`, `Detail`, `Listing`, `_get_json`, `workday_listings`, `workday_detail`, `workday_sitemap_listings`, `workday_sitemap_detail`, `read_source`; structured request outcomes and Workday policy injection |
| `src/scraper.py` | `sources_for`, `scan_company`, `Scan.status/summary`, `_evaluate`, `_scan_sources`, `decide`, `merge_new_jobs`, `run_all`; orchestration, cache and partial diagnostics |
| `src/scan_schedule.py` | `last_scan`, `latest_run`, `plan`, `chain_wait`; run marker independent of per-company rotation |
| `src/scan_handoff.py` | `SHAPES`, `_check`, `take_over`; typed allowlisted state/cache handoff |
| `src/repo_sync.py` | `SCAN_FIELDS`, `merge_companies`, `merge_record`, `publish`; explicit new-state merging and stale-result guard, preserve ledger semantics |
| `src/alerts.py` | `run` initial publication payload only; preserve claim/send/finalize transitions |
| `src/config_store.py`, `src/data_location.py` | Load/save and configured data-root access; preserve remote catalogue and settings ownership |
| `streamlit_app.py` | `portal_status`, `_health`, `page_monitoring`, `tracker_status_card`, `scan_details_rows`; explicit outcomes and scoped health |
| `src/snapshot_cache.py` | Inspect/update only if new state must join the app's coherent freshness snapshot |
| `.github/workflows/check_jobs.yml` | State inputs/artifact outputs, validation, summaries and optional separate health gate |
| `.github/workflows/scan_scheduler.yml` | Sparse state input for scheduler; no cadence change proposed |
| `docs/MONITORING.md`, `docs/SECURITY_GITHUB.md` | Future operational/state/security documentation |

Inspected preservation boundaries: `src/identity.py` (`job_uid`, `ats_job_id`), `src/discovery.py`, `src/user_alerts.py` (`select_jobs`, `run_for_user`), settings and production snapshot data. M6 extraction in `scraper.decide` was inspected; this is not a complete re-audit of every M6 module. Application Tracker and resume modules are regression targets, not planned production edits. `logos.yml`, `application_retention.yml` and `tests.yml` are existing workflow inventory; no changes planned to their behavior.

Proposed new modules: `src/workday_http.py`, `src/workday_cache.py`, `src/workday_scan_state.py`, `src/monitoring_health.py`. Proposed new tests: `tests/test_workday_http.py`, `tests/test_workday_cache.py`, `tests/test_workday_rotation.py`, `tests/test_monitoring_health.py`. Proposed new JSON files are listed above. Only this Markdown plan is created now.

## 8. Staged implementation and rollout

Each stage requires its own implementation review and deployment approval. Later features stay disabled until their acceptance gates pass. Do not combine every change in one implementation PR.

### Stage 0 — Bounded read-only diagnostics and policy assessment

* **Files:** after approval, proposed `scripts/workday_probe.py` and `docs/M7_WORKDAY_PROBE_RESULTS.md`; sanitized fixtures under `tests/fixtures/`. No production data/configuration writes.
* **Implementation:** agree exact companies, official URLs, runner environment and request ceiling first. Recommended initial scope: at most two failing hosts plus JioStar control, one robots request per host, published sitemap index/one sitemap only if permitted, at most one permitted list request and one detail sample per host. No full pagination, retries or scan/email imports. Stop a host immediately on 429/403/challenge; never test a denied path. Record UTC, phase, status, parsed Retry-After, duration and public coverage notes. A separate approved runner trial may compare environment behavior; do not rotate IPs to evade controls.
* **Risks:** even diagnostics consume quota; published alternatives may be incomplete; runner vs local behavior can differ. No site is declared permitted without assessment.
* **Tests:** mock response parsing, strict request ceilings, host/path allowlist, redirects, robots denial, safe output and no-write/no-email guarantees.
* **Acceptance:** bounded reproducible evidence matrix; distinguish verified facts from missing headers/access unknowns; identify whether two alternative-source pilots qualify. No promise all causes can be determined.
* **Rollback:** stop probes; remove diagnostic tooling if unwanted. There are no production mutations to undo.
* **Production impact:** none to scanner behavior; approved external requests have a small nonzero rate-limit impact. **No Stage 0 probes have been run.**

### Stage 1 — Diagnostics and health

* **Files:** `sources.py`, `scraper.py`, `streamlit_app.py`, proposed `monitoring_health.py`, `check_jobs.yml`, `docs/MONITORING.md`; extend health/status/diagnostics tests.
* **Implementation:** optional structured failure metadata; retain legacy-readable status fields; safe company/source summaries; threshold and freshness computation with explicit account scope. Expose incomplete details and pagination caps. Warnings only initially; publication/alerts continue unchanged.
* **Risks:** wrong denominator, stale snapshots, misleading severity and leaked response strings; old UI may not understand new enums. Keep compatibility projection until all readers migrate.
* **Tests:** 26/44 critical; exact 25%/50% boundaries; five failures; empty/all-pending scopes; mixed deferred/cooling prior failures; private scope isolation; malformed/control-character error text; old records with no new fields.
* **Acceptance:** saved incident cannot display complete health; actual attempt/success timestamps remain truthful; safe Actions summary distinguishes scan execution from portal success; identical fixture jobs and ledger inputs.
* **Rollback:** revert UI/aggregation additions; old readers ignore optional fields. Preserve saved last_success and ledger data.
* **Production impact:** diagnostics/display only, no additional source requests or notification channels.

### Stage 2 — Pacing, retry and durable cooldown

* **Files:** proposed `workday_http.py`, `workday_scan_state.py`, state JSON; `sources.py`, `scraper.py`, `scan_schedule.py`, `scan_handoff.py`, `repo_sync.py`, `alerts.py` initial payload, `config_store.py`, relevant app snapshot readers, both scan workflows and security docs.
* **Implementation:** approve tuning, add request controller, first-429 circuit, Retry-After parser, safe deadlines, persistent cooldown and typed partial outcomes. **Move scheduler to run marker now**, before cooldown creates skipped companies. Publish all new state atomically with existing results. Use approved small API canary selection, expand only after evidence; untouched companies retain explicit failed/deferred status, never artificial success.
* **Risks:** runtime growth, skipped work, loss of cooldown on crash, cross-host overblocking, clock skew, handoff failure suppressing delivery. Keep JioStar aggregate breaker separate unless approved otherwise.
* **Tests:** fake monotonic/UTC clocks, integer/date/invalid Retry-After, no early retry, bounded attempts, first detail 429 halts host, PwC shared-host behavior, long delays persisted, restart cooldown, manual dispatch obeys limits, all-cooling run advances window, unknown state fails safely, transactional publication and push conflicts.
* **Acceptance:** no request before allowed time; cooldown survives a new process/run; no subsequent queued details after 429 except designated trial; other adapters unchanged; run persists before timeout; scheduler does not repeatedly dispatch a completed window; shared/personal delivery invariants pass.
* **Rollback:** disable Workday API selection or retain conservative cooldown while reverting new policy. Do not revert to unrestricted repeated requests as an automatic emergency response. Retain valid state files, revert matching producers/readers/handoff as one compatible unit, and verify scheduler fallback. Healthy integrations may continue.
* **Production impact:** fewer Workday requests and possibly longer per-attempt elapsed time, deliberate coverage gaps clearly displayed. No schedule/eligibility/recipient changes.

### Stage 3 — Persistent verdict cache

* **Files:** proposed `workday_cache.py`, cache JSON; `scraper.py`, listing metadata in `sources.py`, `scan_handoff.py`, `repo_sync.py`, initial `alerts.run` payload, `check_jobs.yml`, cache/security tests and docs.
* **Implementation:** versioned cache, approval-selected TTL/bounds, fingerprint invalidation, fair pending queue and full accepted evidence. Begin with fixture shadow comparisons, then small live canary under existing request policy; shadow mode must not double HTTP fetching.
* **Risks:** hidden description changes, stale eligibility, lost M6 evidence, starvation, excessive artifact/repository growth or false duplicate alerts. No cache entry is authoritative for delivery state.
* **Tests:** warm hit/expiry/change/version invalidation; accepted/rejected parity; unreadable never negative-cached; corrupt/missing/oversized cache; malformed URLs; cross-tenant identity; changed job cannot reset ledger; repost sightings and suppression; cold-cache budget fairness; stale prune racing refresh.
* **Acceptance:** unchanged warm fixtures need no detail requests; same eligible identities, evidence, rejection counters and ledger decisions; changed/expired jobs reenter queue; unknown/pending visible; cache survives workflow handoff and first publish; size stays within approved bounds.
* **Rollback:** disable cache reads/writes while preserving pacing and cooldown. Leave cache file inert or remove through a later approved data migration. Do not clear seen jobs or reinitialize delivery claims.
* **Production impact:** warm-run request reduction; cold start and rule-version changes consume bounded extra work; changed-description detection bounded by TTL plus queue/rotation delay, which must be shown rather than promised as instant.

### Stage 4 — Rotation and overdue safeguard

* **Files:** `scraper.py`, proposed `workday_scan_state.py`, `scan_schedule.py`, app health/snapshot readers, scheduler input tests, rotation tests and docs. No cron change required.
* **Implementation:** nine API companies/window if approved, oldest-attempt fairness, new/overdue priority, cooldown filtering and persisted pending state. Publish nominal coverage estimate from actual N/batch/interval. Keep all currently active integrations on their cadence under the recommended scope.
* **Risks:** starvation of deferred/large companies, old scheduler assumptions, catalogue changes and missed runs breaking freshness; request budget may allow fewer than nine.
* **Tests:** 26 and 27 company simulations, PwC two sites, repeated failures, all-cooling, duplicate triggers, six-hour interval, added/removed companies, run cancellation, stale publisher, older code fallback and >24-hour backlog.
* **Acceptance:** in an unconstrained 26-company simulation every company is attempted in three windows; no healthy starvation; cooldown respected; deferred timestamps unchanged; no duplicate planned scan per completed window; 24-hour overdue appears even when cooldown prevents recovery.
* **Rollback:** turn off rotation while retaining request controller/cooldowns/deadlines and new run marker. If all-company scanning exceeds safe budget, defer explicitly rather than raising traffic. Keep state for later resumption.
* **Production impact:** normal API revisit target changes from three to nine hours; outages can exceed this. Shared/personal delivery cadence and cutoffs stay unchanged.

### Stage 5 — Two permitted alternative-source pilots

* **Files:** `sources.py`, per-company configuration in `scraper.COMPANY_SOURCES` only after approval, permitted-source fixtures/tests, `docs/MONITORING.md` and probe evidence matrix. `identity.py` should not change unless fixtures prove an unavoidable, separately reviewed identity issue.
* **Implementation:** choose two Stage 0-qualified failing companies; implement official sitemap/feed parsing, completeness diagnostics and allowed detail retrieval through the same request policy/cache. Compare saved/fixture identities and publicly observable coverage; do not call blocked APIs to construct a baseline. Expand only after both pilots demonstrate acceptable coverage.
* **Risks:** partial sitemaps, delayed listings, URL/ID drift, revised access policies, cross-host redirects and country filtering losses.
* **Tests:** indices/namespaces/pagination, malformed/truncated/empty responses, host restrictions, identity matching, blocked paths never called, valid India evidence, same requisition never re-alerted, JioStar regression.
* **Acceptance:** two explicitly approved companies show permitted, repeatable discovery with disclosed coverage; unchanged eligibility and duplicate behavior; no loss of the 18 active integrations. If two suitable alternatives do not exist, document the blocker rather than fabricate a fallback.
* **Rollback:** disable the new source mapping; restore the old mapping only if permitted and cooldown-safe, otherwise leave unavailable with a truthful diagnostic. Keep existing accepted jobs and all delivery history.
* **Production impact:** two companies' discovery channels change; possible coverage/freshness differences require approval before broader rollout.

## 9. Test strategy and planning validation

During this planning task, ran only the following existing local fixture tests, with bytecode and pytest cache writes disabled:

```text
python -m pytest -q -p no:cacheprovider tests/test_scraper.py tests/test_jiostar_sitemap_2026_11.py tests/test_scan_diagnostics_2026_11.py
73 passed in 1.03s
```

These tests use mocked HTTP/local objects. They establish a narrow baseline, not live recovery or full application certification. No scraper CLI, alerts CLI, production scan, browser probe or email was run.

Future implementation regression gates:

| Concern | Existing suites to preserve/extend |
|---|---|
| Source correctness and failure classification | `test_sources.py`, `test_scraper.py`, `test_failing_companies_2026_11.py`, `test_jiostar_sitemap_2026_11.py`, `test_adp_feed_guard.py`, `test_scan_diagnostics_2026_11.py` |
| Eligibility and stable identity | `test_golden_set.py`, `test_adversarial_classifier.py`, `test_job_classifier.py`, `test_locations_identity.py`, `test_posting_dates_2026_11.py`, `test_repost_window.py` |
| Scheduling / truthful UI | `test_scan_status_p0.py`, `test_scan_schedule_and_settings.py`, `test_scan_scheduler_chain.py`, `test_catalogue_watchlist.py` |
| Shared/personal delivery and isolation | `test_alerts.py`, `test_user_alerts.py`, `test_delivery_reliability.py`, `test_workflow_personal_alerts.py`, `test_admin_scopes.py`, `test_account1_monitoring.py`, `test_email_tailoring.py`, `test_alert_cutoff_2026_11.py` |
| Handoff, persistence and rollback | `test_jt01_supply_chain.py`, `test_github_privilege_boundaries.py`, `test_phase2_security.py`, `test_rollback_compat.py`, `test_config_store.py`, `test_snapshot_cache.py`, `test_repo_privacy.py` |
| Application Tracker and M6 | `test_application_tracker.py`, `test_m6_app.py`, `test_m6_extraction.py`, `test_m6_matching.py`, `test_m6_security.py`, `test_m6_performance.py`, `test_m6_eval.py` and remaining M6 suites |

Inventory of tests is not a claim all were run or exhaustively audited. Implementation CI should run the full appropriate existing suite after targeted tests, using fake SMTP/Firestore/Git publishers and no external career traffic. Add property/simulation tests for queue fairness and monotonic state merging. Compare the same fixture population with cache disabled/enabled, not merely increased job counts. Test mixed old/new schemas and rollback explicitly. Production observation only occurs after approval; use at least one full rotation and two cache lifetimes before declaring a stage stable, with the exact canary duration approved in advance.

## 10. Rollback and release safety

Use separate switches for diagnostics rendering, Workday selection/pacing policy, cache and rotation; their production values need approval. Keep a safe “pause Workday API requests, continue unrelated scanners” option. Turning off optimization must not implicitly disable cooldown or create a cold-cache request storm.

Before each approved release, record code SHA, data schema version and last good data commit. Roll back code and producer/consumer schema expectations together. Preserve current remote data: **never reset production data to an old commit** as a generic rollback, because that can undo dismissals and delivery claims and resend mail. Forward-compatible optional fields and explicit version migration are preferable. Restore only a validated derived cache/state component when necessary, keeping conservative host cooldowns and monotonic run markers. No rollback resets `seen_jobs.json` or Firestore ledgers.

For a broken handoff stop publication and investigate with sanitized artifacts; do not bypass validation. For false-positive health warnings revert rendering/thresholds, not source evidence. For a bad alternative adapter pause those two sources and retain their jobs/history. Before resuming, validate scheduler due logic, current cooldowns, recipient isolation and no duplicate alerts with local fixtures.

## 11. Acceptance criteria for M7 completion

1. All 44 companies remain accounted for; 26 failing API companies are either demonstrably recovered through permitted discovery or explicitly unresolved with evidence. An unresolved policy block is not called a successful recovery. Preserve all 18 previously active integrations.
2. Every actual Workday request obeys configured global/host spacing and server cooldown; bounded retries and one half-open trial are proven with deterministic tests. No denial bypass.
3. Repeated unchanged postings avoid redundant details; TTL/version changes invalidate; all unprocessed candidates remain visible and eventually eligible for fair retry. No silent negative caching.
4. Normal unconstrained API rotation meets the approximate nine-hour attempt target under the approved three-hour cadence; 24-hour success/coverage breaches are visible, even during cooldown. No fake last_checked or last_success updates.
5. The saved 26/44 incident displays critical health. Deferred, rate-limited, cooling and never-checked states cannot produce unqualified complete health. Actions reports portal health separately from successful execution/publication.
6. New state survives validated cross-job transport and merge-safe publishing; corruption, missing files, concurrent app edits, stale runs and rollback are tested. No credentials or personal data enter artifacts/logs.
7. Shared/personal recipients, preferences, cutoffs, deduplication, claims, uncertain delivery and retries are unchanged. Eligibility/golden set, Application Tracker and M6 regressions pass.
8. Runtime and artifact size stay within approved budgets. Real recovery and request reduction are measured over approved observation windows, not inferred from faster failing scans or increased accepted-job counts.

## 12. Outstanding decisions requiring approval

No item below is silently activated by accepting this planning document.

| Decision | Recommendation and trade-off |
|---|---|
| Stage 0 execution | Approve exact two failing hosts + JioStar control, URLs, request ceiling and local/runner environment separately. Small probes reduce quota impact but cannot prove global rate-limit scope. |
| Access-policy uncertainty | Confirm how unknown/ambiguous terms are resolved before requests; prefer pause/review over assuming permission. |
| Stage-by-stage implementation/deployment | Review each stage separately; no implementation PR, commit, push or deployment is authorized here. |
| Pacing and budgets | Pilot 1s global / 2s host / 10s between companies with measured runtime limits; slower is safer but worsens coverage. Approve request/time ceilings and whole-run deadline. |
| Retry/cooldown | Up to two retries, 30s/60s fallback, 120s in-run wait ceiling, 3h→24h fallback cooldown; valid Retry-After wins. Fewer retries reduce pressure but defer transient recoveries. |
| Provider-wide circuit | Consider three distinct API hosts returning 429; decide threshold and whether sitemap traffic is excluded. Broad protection risks unnecessary pauses. |
| Cache TTL/version/retention | Recommend 24h verdict TTL, explicit rule/extraction versions, bounded entries/bytes and expiry pruning; approve exact size/retention limits after inventory. Shorter TTL finds invisible detail changes sooner at higher traffic cost. |
| Pending-work allocation | Approve new/changed priority with reserved aged-work share (candidate 75/25); strict new-first alone can starve existing jobs. |
| Rotation scope and freshness | Nine of 26 API companies per three-hour run; leave JioStar on existing cadence. Accept nominal nine-hour revisit and truthful >24h breaches; do not auto-expand budgets during an outage. |
| Health policy | Approve 25% or five degraded / 50% critical, separate coverage bands, scope visibility and warning-only Actions rollout versus a later post-publication failing health check. No new email channel proposed. |
| Storage and actual repository controls | Recommend artifact transport + explicit git data persistence; inspect actual `JT_DATA_BRANCH`, production environment and branch rules before deployment. Dedicated data branch changes need separate approval. |
| Canary / observation window | Select API canaries and approve live rollout duration (at least full rotation/two TTLs recommended), recovery and request-reduction targets based on Stage 0 evidence. |
| Alternative-source pilots | Approve two evidence-qualified failing companies and acceptable disclosed coverage; Sanofi/PwC are assessment candidates only. No eligible pair means no rollout. |
| Emergency rollback | Approve safe Workday API pause with healthy scanners continuing; avoid automatic return to unpaced full scans. |

## 13. Handover status

This document completes the requested planning deliverable. It was written on the separate documentation branch, with no production implementation, configuration, JSON data, schedule, eligibility, M6 or alert-setting changes. No live probes, email sends, commits, pushes, implementation PRs or deployments were performed. Recovery itself remains unimplemented and unverified pending the approvals above.
