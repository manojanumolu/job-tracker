"""Quoted posting text kept as evidence never carries an email address into
the public data files (S&P Global's "report fraud to ...@spglobal.com" line,
20 Nov 2026). No network."""
import scraper
from sources import Detail, Listing

DESC = ("Apprentice, Client Support in Hyderabad, India. Freshers welcome; 0 years of experience required. "
        "Requirements: graduate degree; good communication. If you receive an offer asking for payment it is a "
        "scam and should be reported to reportfraud@spglobal.com . For accommodation email EEO.Compliance@spglobal.com.")


def test_accepted_job_evidence_has_no_email_address():
    listing = Listing(title="Apprentice, Client Support", url="https://careers.spglobal.com/careers-home/jobs/332488",
                      location="Hyderabad, India", job_id="332488", posting_evidence=True)
    detail = Detail(ok=True, description=DESC, location="Hyderabad, India", posting_evidence=True, matched="job page")
    _, job = scraper.decide("S&P Global", listing, detail)
    assert job is not None
    text = repr(job)
    assert "@" not in text.replace("[email removed]", "")
    assert not scraper._EMAIL_RE.search(text)


def test_redaction_keeps_everything_else():
    value = {"a": ["mail x.y@corp.co.in now", 3], "b": "no address", "c": None}
    assert scraper._redact_emails(value) == {"a": ["mail [email removed] now", 3], "b": "no address", "c": None}
