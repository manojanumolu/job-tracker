import unittest
from unittest.mock import patch

from sources import SourceError, jibe_listings


class JibeCoverageTests(unittest.TestCase):
    host = {"host": "https://careers.example"}
    job = {"data": {"country": "Germany", "slug": "one"}}

    def read(self, pages):
        with patch("sources._get_json", side_effect=pages):
            return jibe_listings(None, self.host)

    def test_empty_final_page_before_reported_total(self):
        with self.assertRaisesRegex(SourceError, "1 of 2"):
            self.read([{"jobs": [self.job], "totalCount": 2}, {"jobs": []}])

    def test_page_cap_without_end_of_results(self):
        with patch("sources.JIBE_MAX_PAGES", 1):
            with self.assertRaisesRegex(SourceError, "pagination limit"):
                self.read([{"jobs": [self.job]}])

    def test_exact_total_on_last_allowed_page(self):
        with patch("sources.JIBE_MAX_PAGES", 1):
            result = self.read([{"jobs": [self.job], "totalCount": 1}])
        self.assertEqual(result.seen, 1)
        self.assertEqual(result.listings, [])

    def test_reported_empty_is_valid(self):
        self.assertEqual(self.read([{"jobs": [], "totalCount": 0}]).seen, 0)
