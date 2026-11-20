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
