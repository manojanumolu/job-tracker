"""Salesforce's official job report (the JSON its careers site reads from its
CDN): one request, India by Countries, inline descriptions, JR ids, Workday
posting URLs kept so records match. Fixtures; no network."""
import json

import httpx
import pytest

import scraper
import sources

URL = "https://a.sfdcstatic.example/careers/prod/jobs_2.json"
DESC = "<p>Associate Software Engineer in Hyderabad. Freshers welcome; 0-1 years of experience.</p>" * 3


def _row(i, countries=("India",), primary="India - Hyderabad", desc=DESC):
    return {"Job_Requisition_Ref_ID": f"JR3{i:05d}", "Job_Posting_Title": f"Associate Software Engineer {i}",
            "Job_Requisition_Primary_Location": primary, "Countries": list(countries), "Locations": ["Hyderabad"],
            "External_Job_Posting_Start_Date": "2026-11-18", "Job_Description": desc,
            "External_Job_Posting_Site": f"https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/India---Hyderabad/Associate_JR3{i:05d}"}


def _client(rows, count=None, total=None, status=200):
    body = {"Fetch_Timestamp": "2026-11-20T08:28+00:00", "Count": len(rows) if count is None else count,
            "Total_Jobs": len(rows) if total is None else total, "Report_Entry": rows}
    asked = []

    def handler(request):
        asked.append(request.url)
        return httpx.Response(status, text=json.dumps(body))
    return httpx.Client(transport=httpx.MockTransport(handler)), asked


def test_one_request_india_only_with_inline_detail_and_requisition_ids():
    client, asked = _client([_row(1), _row(2, countries=("Ireland",), primary="Ireland - Dublin"),
                             _row(3, countries=("United States", "India"), primary="USA - Remote")])
    res = sources.report_feed_listings(client, {"url": URL, "country": "India"})
    assert len(asked) == 1 and res.seen == 3 and [l.job_id for l in res.listings] == ["JR300001", "JR300003"]
    first = res.listings[0]
    assert first.detail.ok and first.detail.posted_at == "2026-11-18" and "Freshers welcome" in first.detail.description
    assert first.url.startswith("https://salesforce.wd12.myworkdayjobs.com/") and first.location == "Hyderabad, India"
    assert first.fetch_detail is None                                    # nothing else is requested


def test_a_partial_or_failed_feed_is_never_healthy():
    client, _ = _client([_row(1)], total=1617)
    with pytest.raises(sources.SourceError, match="incomplete coverage"):
        sources.report_feed_listings(client, {"url": URL})
    client, _ = _client([_row(1)], status=503)
    with pytest.raises(sources.SourceError, match="HTTP 503"):
        sources.report_feed_listings(client, {"url": URL})
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>maintenance</html>")))
    with pytest.raises(sources.SourceError, match="unexpected format"):
        sources.report_feed_listings(bad, {"url": URL})


def test_a_posting_without_description_is_unreadable_not_accepted():
    client, _ = _client([_row(4, desc="")])
    listing = sources.report_feed_listings(client, {"url": URL}).listings[0]
    assert not listing.detail.ok


def test_salesforce_uses_its_official_feed():
    assert scraper.sources_for({"id": "c1793696368", "url": "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"}) == [
        {"type": "report_feed", "url": "https://a.sfdcstatic.com/digital/xsf/careers/prod/jobs_2.json", "country": "India"}]
