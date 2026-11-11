# Golden set — labelled postings for the eligibility classifier

`golden_set.json` is the measurable gate on discovery quality (Nov 2026).
`tests/test_golden_set.py` runs every case through
`job_classifier.classify_job` on every test run and in CI, and prints:

```
[golden] cases=222 (...) · precision=… recall=… false_rejection_rate=… · fp=… fn=… hard_fn=… drift=…
```

## The gates

| Gate | Fails when | Protects against |
|---|---|---|
| precision | a `reject` or `unknown` case is accepted | weakening an eligibility rule to find more jobs |
| recall | an `accept` case is no longer accepted | losing postings the pipeline really alerted on |
| false rejection | an `accept` case is hard-rejected (EXPERIENCED / SENIOR / NOT_A_JOB) | the worst kind of lost job, named separately |
| snapshot | a `reject`/`unknown` case changes category, or a `borderline` case changes verdict | silent behaviour drift — every change is a reviewed dataset edit |

A mutation check (Nov 2026) confirmed each kind of regression is caught:
removing the seniority rule, treating "Associate" as entry evidence,
ignoring experience requirements, dropping the "freshers need not apply"
rule, and disabling the fresher/entry description signals each fail at
least one gate.

## Cases

Each case: `id`, `source`, `company`, `title`, `description`, `url`,
`expect` (`accept` · `reject` · `unknown` · `borderline`), `category`
(the expected verdict; `current` for borderline), `tags`, `notes`.

| Source | Cases | What it is |
|---|---|---|
| `production/seen_jobs.json` | 109 | Every record the live pipeline accepted (as of 2026-11-11). The description is rebuilt from the record's stored `evidence.experience_lines` and fresher quote — real posting text. 3 records whose lines, out of context, read as a requirement are kept as `borderline` (tag `reconstruction-artifact`): the full posting was accepted, the excerpt alone isn't. |
| `production/scan-log-run-42969601176` | ~70 | Real rejected/unknown decisions from the 2026-11-11 06:02 UTC production scan log (the last ~11 portals the log API returns). Title-level rejections keep the title only; requirement rejections carry the quoted requirement as their description. Fetch outcomes (unreadable page, safety gate) are left out: they aren't text classification. |
| `curated` | ~43 | Hand-written cases for documented rules and their edges: durations vs requirements, company/colleague boilerplate, preferred sections, broad ranges, staff vs applicant context, levelled and MTS titles, story/landing/picker pages, adversarial senior titles with fresher-sounding text, and the role-named `Role: …` titles. |

The cases are frozen copies: the test never reads live data, so a scan
can't change the gate.

## Changing the classifier

1. Run `python -m pytest -s tests/test_golden_set.py` before and after.
2. Precision must stay 1.000 — no exceptions. A rule that only "finds more
   jobs" by accepting a labelled reject is weakening eligibility.
3. If a verdict moves on purpose (a fix), edit those cases here in the same
   commit and say why in `notes`. Never relabel a case just to make it pass.
4. Add the case that motivated the change, with the posting text that
   shows it.

## Limits

* Accepted cases come from what the pipeline already accepted, so they
  prove no regression, not absolute recall. Independent recall comes from
  hand-reviewing rejections: the Nov 2026 review found that role-named
  `Role: specialisation` titles (Cisco, Qualcomm) were discarded as
  "career stories" — fixed, with cases added. Repeat that review from
  each scan's `rejections` statistics.
* Rejected log cases carry a title and at most the quoted requirement, not
  the full posting.
