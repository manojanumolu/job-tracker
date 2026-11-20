import json
import tempfile
import unittest
from pathlib import Path

from workday_state import CACHE_FILE, empty, merge, write


class CachePrivacyTests(unittest.TestCase):
    def test_artifact_and_publication_redact_legacy_and_new_entries(self):
        stamp = "2026-11-20T06:00:00+00:00"
        address = "contact@example.invalid"
        detail = dict(ok=True, description=address, strict_text=address, location="India",
                      posted_at="", posting_evidence=True, reason=address, matched="ATS")
        key = "a" * 64
        cache = empty(CACHE_FILE)
        cache.update(updated_at=stamp, run_id="run")
        entry = dict(company_id="example", source=key, rules=key, fingerprint=key,
                     evaluated_at=stamp, expires_at="2026-11-23T06:00:00+00:00",
                     title=address, detail=detail, verdict="accepted")
        cache["entries"][key] = entry
        safe_key = "b" * 64
        cache["entries"][safe_key] = {**entry, "title": "Engineer", "detail": {
            **detail, "description": "Public qualifications", "strict_text": "", "reason": ""}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / CACHE_FILE
            write(path, CACHE_FILE, cache)
            artifact = json.loads(path.read_text())
        clean = {**cache, "entries": {}}
        for result in (artifact, merge(CACHE_FILE, None, cache), merge(CACHE_FILE, cache, clean)):
            self.assertNotIn(address, json.dumps(result))
            self.assertEqual(set(result["entries"]), {safe_key})
            self.assertEqual(result["entries"][safe_key], cache["entries"][safe_key])
        self.assertEqual(entry["detail"]["description"], address)
