import unittest
from unittest.mock import patch

from sources import jibe_listings


class JibeCoverageTests(unittest.TestCase):
    host = {"host": "https://careers.example"}
    job = {"data": {"country": "Germany", "slug": "one"}}

    def read(self, pages):
        with patch("sources._get_json", side_effect=pages):
            return jibe_listings(None, self.host)

    def test_empty_final_page_before_reported_total(self):
        row = {"data": {"country": "India", "title": "Engineer", "slug": "one"}}
        result = self.read([{"jobs": [row], "totalCount": 2}, {"jobs": []}])
        self.assertIn("1 of 2", result.coverage_error)
        self.assertEqual([job.title for job in result.listings], ["Engineer"])

    def test_page_cap_without_end_of_results(self):
        with patch("sources.JIBE_MAX_PAGES", 1):
            result = self.read([{"jobs": [self.job]}])
        self.assertIn("pagination limit", result.coverage_error)

    def test_exact_total_on_last_allowed_page(self):
        with patch("sources.JIBE_MAX_PAGES", 1):
            result = self.read([{"jobs": [self.job], "totalCount": 1}])
        self.assertFalse(result.coverage_error)
        self.assertEqual(result.seen, 1)
        self.assertEqual(result.listings, [])

    def test_reported_empty_is_valid(self):
        self.assertEqual(self.read([{"jobs": [], "totalCount": 0}]).seen, 0)


def test_amd_is_filtered_to_india_server_side_and_paced(monkeypatch):
    import httpx
    import scraper
    import sources
    asked, waits = [], []
    monkeypatch.setattr(sources.time, "sleep", lambda s: waits.append(s))

    def handler(request):
        asked.append(dict(request.url.params))
        page = int(request.url.params["page"])
        jobs = [{"data": {"title": f"Engineer {page}-{i}", "city": "Bengaluru", "country": "India", "slug": f"{page}{i}",
                          "req_id": f"{page}{i}", "description": "<p>Graduate role, freshers welcome.</p>"}} for i in range(10 if page < 3 else 1)]
        return httpx.Response(200, json={"totalCount": 21, "jobs": jobs})
    cfg = scraper.COMPANY_SOURCES["amd"][0]
    assert cfg["location"] == "India" and cfg["page_delay_s"] == 5
    res = sources.jibe_listings(httpx.Client(transport=httpx.MockTransport(handler)), cfg)
    assert res.seen == 21 and all(p["location"] == "India" for p in asked) and waits == [5, 5]
    assert not getattr(res, "coverage_error", "")
