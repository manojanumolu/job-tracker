"""Audit follow-up (Nov 2026): each scan keeps why postings were not
accepted, and how many reposts it suppressed, so a company that never
yields a job can be judged from the data. Measurement only: nothing is
accepted or rejected differently. No network."""
from collections import Counter

import scraper
import sources

D = "Design and build distributed services used by millions of customers. "


def test_a_scan_keeps_why_postings_were_not_accepted():
    good = D + "Freshers welcome. 0-1 years of experience. " * 3
    def listing(title, text):
        detail = sources.Detail(ok=True, description=text, location="Hyderabad, India", posting_evidence=True, matched="t")
        return sources.Listing(title=title, url=f"https://x.example/{abs(hash(title))}", location="Hyderabad, India",
                               posting_evidence=True, detail=detail)
    scan = scraper.Scan("X")
    for lst in (listing("Senior Software Engineer", good), listing("Lead Data Engineer", good),
                listing("Software Engineer", D + "5+ years of experience."), listing("Software Engineer", D),
                listing("Graduate Software Engineer", good)):
        scraper._evaluate(scan, lst, [10])
    s = scan.summary()
    assert s["accepted"] == 1 and s["rejected"] == 3 and s["unknown"] == 1
    assert s["rejections"] == {"seniority in title": 2, "explicit requirement": 1, "no fresher/entry-level evidence": 1}
    assert not any("Senior" in k or "Engineer" in k for k in s["rejections"])       # kinds only, never a job's text


def test_reposts_suppressed_are_counted_not_changed():
    seen = [{"title": "Software Engineer", "url": "https://x.example/job/1", "company": "X", "location": "Hyderabad · India",
             "uid": "x|url:x.example/job/1"}]
    repost = {"title": "Software Engineer", "url": "https://x.example/job/2", "company": "X", "location": "Hyderabad · India"}
    stats = Counter()
    assert scraper.merge_new_jobs(seen, [repost], stats=stats) == [] and stats["reposts_suppressed"] == 1
    assert scraper.merge_new_jobs(seen, [repost]) == []                                # same behaviour without stats
