"""M6 Part 13 — the accuracy evaluation as a regression guard. The floors
are below the measured values (docs/RESUME_INTELLIGENCE.md has the
numbers and the failing cases); a change that lowers accuracy fails here."""
from m6_eval import evaluate, report


def test_the_evaluation_stays_above_its_floors():
    m = evaluate()
    assert m["size"] == {"resumes": 12, "jobs": 8, "pairs": 24}
    floors = {"skill_precision": 0.95, "skill_recall": 0.95, "demonstrated_precision": 0.95, "demonstrated_recall": 0.9,
              "mandatory_extraction_precision": 0.9, "mandatory_extraction_recall": 0.95, "matching_precision": 0.95,
              "matching_recall": 0.95, "eligibility_accuracy": 0.95, "evidence_accuracy": 0.9,
              "graduation_year_accuracy": 0.9, "employment_months_accuracy": 0.9}
    low = {k: round(m[k], 3) for k, f in floors.items() if m[k] < f}
    assert not low, report(m)
    assert m["false_positive_rate"] < 0.01 and m["score_consistency"] == 1.0


def test_the_report_lists_every_metric_and_the_failing_cases():
    text = report(evaluate())
    for label in ("Skill extraction precision", "Mandatory matching recall", "False-positive rate", "Eligibility accuracy",
                  "Evidence attribution accuracy", "Score consistency"):
        assert label in text
    assert "Failing cases" in text                     # the misses are shown, not hidden
