"""The labelled golden set: the measurable gate on discovery quality.

tests/golden/golden_set.json holds ~210 labelled postings (see its README
for provenance — production records, production scan-log decisions and
curated borderline cases). Every classifier change runs against it here:

  * precision gate — no "reject" or "unknown" case may ever be accepted
    (an accepted prediction that should not alert is a false positive);
  * recall gate — every "accept" case stays accepted (these are real
    postings the pipeline alerted on; losing one is a regression);
  * false-rejection gate — no "accept" case may be HARD-rejected
    (EXPERIENCED / SENIOR / NOT_A_JOB): a true positive degrading to
    UNKNOWN already fails the recall gate, but a hard reject is worse,
    so it is named separately;
  * category snapshots — "reject"/"unknown" cases assert their category
    and "borderline" cases their recorded current verdict, so any shift
    is a deliberate, reviewed edit to the dataset, never an accident.

The metrics (precision, recall, false-rejection rate, per-source counts)
are printed on every run, so a change's effect is visible in CI output
even when every gate passes. Weakening an eligibility rule to accept more
jobs shows up here as a precision failure — the rule the project set for
itself, now enforced by CI.
"""
import json
from collections import Counter
from pathlib import Path

import pytest

from job_classifier import classify_job

GOLDEN = Path(__file__).parent / "golden" / "golden_set.json"
HARD_REJECT = {"EXPERIENCED", "SENIOR", "NOT_A_JOB"}


def _cases() -> list[dict]:
    data = json.loads(GOLDEN.read_text("utf-8"))
    assert data["version"] == 1
    cases = data["cases"]
    assert len(cases) >= 200, "the golden set must stay a real corpus"
    assert len({c["id"] for c in cases}) == len(cases), "case ids must be unique"
    return cases


def _verdict(case: dict):
    return classify_job(case["title"], case.get("description", ""), url=case.get("url", ""),
                        posting_evidence=True)


def _metrics(cases):
    out = {"tp": [], "fn": [], "fp": [], "hard_fn": [], "snapshot_drift": []}
    for c in cases:
        r = _verdict(c)
        if c["expect"] == "accept":
            (out["tp"] if r.accepted else out["fn"]).append((c, r))
            if r.category.value in HARD_REJECT:
                out["hard_fn"].append((c, r))
        elif c["expect"] in ("reject", "unknown"):
            if r.accepted:
                out["fp"].append((c, r))
            elif r.category.value != c["category"]:
                out["snapshot_drift"].append((c, r))
        elif c["expect"] == "borderline":
            if r.category.value != c["current"]:
                out["snapshot_drift"].append((c, r))
    return out


def _describe(pairs, limit=12):
    return "\n".join(f"  {c['id']} · {c['title'][:70]!r} -> {r.category.value}: {r.reason[:90]}"
                     for c, r in pairs[:limit])


@pytest.fixture(scope="module")
def results():
    cases = _cases()
    m = _metrics(cases)
    accepts = [c for c in cases if c["expect"] == "accept"]
    predicted_accept = len(m["tp"]) + len(m["fp"])
    precision = len(m["tp"]) / predicted_accept if predicted_accept else None
    recall = len(m["tp"]) / len(accepts) if accepts else None
    false_rejection = len(m["hard_fn"]) / len(accepts) if accepts else None
    by_source = Counter(c["source"].split("/")[0] for c in cases)
    print(f"\n[golden] cases={len(cases)} ({dict(by_source)}) · "
          f"precision={precision:.3f} recall={recall:.3f} false_rejection_rate={false_rejection:.3f} · "
          f"fp={len(m['fp'])} fn={len(m['fn'])} hard_fn={len(m['hard_fn'])} drift={len(m['snapshot_drift'])}")
    return m


def test_precision_no_labelled_reject_or_unknown_is_ever_accepted(results):
    assert not results["fp"], (
        "cases that must never alert were ACCEPTED (an eligibility rule was weakened):\n" + _describe(results["fp"]))


def test_recall_every_production_accepted_posting_stays_accepted(results):
    assert not results["fn"], (
        "real alerted postings are no longer accepted:\n" + _describe(results["fn"]))


def test_no_true_positive_is_hard_rejected(results):
    assert not results["hard_fn"], (
        "real alerted postings are now hard-rejected (worse than UNKNOWN):\n" + _describe(results["hard_fn"]))


def test_rejection_categories_and_borderline_snapshots_hold(results):
    assert not results["snapshot_drift"], (
        "verdicts moved for labelled cases — review each and update the dataset deliberately:\n"
        + _describe(results["snapshot_drift"]))
