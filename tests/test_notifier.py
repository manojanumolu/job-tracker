import re
from email import message_from_string
from html.parser import HTMLParser

import pytest

import notifier
from job_classifier import classify_job
from notifier import build_alert_email, build_test_email, friendly_reason, safe_url


def _job(**kw):
    job = {
        "title": "Graduate Software Engineer",
        "company": "Sanofi",
        "location": "Hyderabad · India",
        "url": "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers/job/Hyderabad/GSE_R1",
        "category": "ENTRY_LEVEL",
        "reason": "entry-level signal: 'Graduate'",
    }
    job.update(kw)
    return job


class _Checker(HTMLParser):
    """Tag-balance check: every opened non-void tag is closed in order."""
    VOID = {"meta", "br", "img", "hr", "input", "link"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.links = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs).get("href"))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> (open: {self.stack[-3:]})")
        else:
            self.stack.pop()


def _parse(html):
    checker = _Checker()
    checker.feed(html)
    checker.close()
    assert not checker.errors, checker.errors
    assert checker.stack == [], f"unclosed tags: {checker.stack}"
    return checker


def _visible_text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_single_job_email():
    subject, html, text = build_alert_email([_job()])
    assert subject == "🎯 New fresher job: Graduate Software Engineer at Sanofi"
    checker = _parse(html)
    assert checker.links == [_job()["url"]]
    body = _visible_text(html)
    for part in ("Graduate Software Engineer", "Sanofi", "Hyderabad · India", "Entry level", "View job"):
        assert part in body
    assert "JOB 1 OF" not in body  # counter only for multi-job emails
    assert "<!DOCTYPE html>" in html and 'name="viewport"' in html
    assert "max-width:600px" in html  # mobile: fluid width capped at 600px


def test_multiple_jobs_have_separate_sections_and_buttons():
    jobs = [
        _job(),
        _job(title="Trainee Analyst", company="PwC", url="https://pwc.example/jobs/2",
             category="FRESHER", reason="fresher signal: 'Freshers welcome'"),
        _job(title="Associate Developer", company="Avalara", url="https://avalara.example/j/3",
             category="FRESHER", reason="experience starts at 0: '0-1 years'"),
    ]
    subject, html, text = build_alert_email(jobs)
    assert subject == "🎯 3 new fresher jobs — Sanofi, PwC, Avalara"
    checker = _parse(html)
    assert checker.links == [j["url"] for j in jobs]
    for i in (1, 2, 3):
        assert f"JOB {i} OF 3" in html and f"JOB {i} OF 3" in text
    assert html.count("View job") == 3
    assert text.count("View job: ") == 3
    assert text.count("-" * 40) == 2  # separators between the three jobs


def test_subject_lists_at_most_three_companies():
    jobs = [_job(company=f"Co{i}", url=f"https://x.example/{i}") for i in range(5)]
    assert build_alert_email(jobs)[0] == "🎯 5 new fresher jobs — Co0, Co1, Co2 and more"


def test_missing_optional_fields():
    minimal = {"title": "Graduate Trainee", "url": "https://example.com/j/1"}
    subject, html, text = build_alert_email([minimal])
    _parse(html)
    assert subject == "🎯 New fresher job: Graduate Trainee"
    assert "Location: See posting" in text
    assert "Fresher / entry level" in html and "Fresher / entry level" in text
    assert "Entry-level / graduate role" in text  # generic reason
    assert "Company:" not in text

    # a record with nothing at all still renders
    subject, html, text = build_alert_email([{}])
    _parse(html)
    assert "Untitled posting" in html and "Untitled posting" in text
    assert "View job" not in html
    assert "Open the company's career page to apply." in text


def test_special_characters_are_escaped():
    job = _job(title='Engineer <script>alert("x")</script> & "R&D"',
               company="Johnson & Johnson <India>", location="Pune <br> India",
               reason="fresher signal: 'Freshers <b>welcome</b>'")
    subject, html, text = build_alert_email([job])
    _parse(html)
    assert "<script>" not in html and "<b>welcome" not in html and "<br> India" not in html
    assert "Engineer &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &quot;R&amp;D&quot;" in html
    assert "Johnson &amp; Johnson &lt;India&gt;" in html
    # plain text keeps the original characters
    assert 'Engineer <script>alert("x")</script> & "R&D"' in text
    assert "Johnson & Johnson <India>" in text


def test_url_with_query_parameters_is_escaped_and_intact():
    url = 'https://jobs.example.com/view?id=42&src=alert&q="x"'
    subject, html, text = build_alert_email([_job(url=url)])
    assert 'href="https://jobs.example.com/view?id=42&amp;src=alert&amp;q=&quot;x&quot;"' in html
    assert _parse(html).links == [url]  # the parser unescapes back to the real URL
    assert f"View job: {url}" in text


@pytest.mark.parametrize("url", ["javascript:alert(1)", "", "/relative/path", "ftp://x.example/a",
                                 "data:text/html,hi", "https://"])
def test_unsafe_or_missing_urls_get_no_button(url):
    assert safe_url(url) == ""
    subject, html, text = build_alert_email([_job(url=url)])
    assert "View job" not in html and "javascript:" not in html
    assert _parse(html).links == []
    assert "View job:" not in text


def test_categories_are_distinguished():
    fresher = build_alert_email([_job(category="FRESHER", reason="fresher signal: 'Freshers'")])
    entry = build_alert_email([_job(category="ENTRY_LEVEL")])
    assert ">Fresher<" in fresher[1] and "Category: Fresher" in fresher[2]
    assert ">Entry level<" in entry[1] and "Category: Entry level" in entry[2]
    assert "#dcfce7" in fresher[1] and "#dbeafe" in entry[1]  # different badge colours


@pytest.mark.parametrize(
    "title, description, expected",
    [
        ("Software Engineer", "Experience: 0-1 years", "0–1 years experience"),
        ("Software Engineer", "Years of Experience: 0-2", "0–2 years experience"),
        ("Software Engineer", "Experience: 0 - 2 Years", "0–2 years experience"),
        ("Software Engineer", "Experience: 0+ years", "No prior experience required"),
        ("Associate", "No experience required.", "No prior experience required"),
        ("Associate", "This role does not require prior experience.", "No prior experience required"),
        # (no longer echoes the bare title word: "Open to freshers — “Fresher”")
        ("Fresher Software Engineer", "", "Open to freshers (stated in the job title)"),
        ("Associate", "Freshers welcome", "Open to freshers — “Freshers welcome”"),
        ("Graduate Software Engineer", "", "Entry-level / graduate role — “Graduate”"),
        ("Associate", "This is an entry-level position.",
         "Entry-level / graduate role — “This is an entry-level”"),
    ],
)
def test_friendly_reason_from_real_classifier_output(title, description, expected):
    result = classify_job(title, description)
    assert result.accepted
    job = {"category": result.category.value, "reason": result.reason}
    assert friendly_reason(job) == expected


@pytest.mark.parametrize("reason", [
    "REJECTED [EXPERIENCED] — explicit requirement: '2-4 years'",
    "Classification(category=<Category.FRESHER>)", "", None, "{'raw': 'json'}",
])
def test_no_internal_wording_leaks(reason):
    job = _job(reason=reason, category="FRESHER")
    subject, html, text = build_alert_email([job])
    for leak in ("REJECTED", "ACCEPTED", "EXPERIENCED", "Category.", "Classification(", "{'raw'",
                 "ENTRY_LEVEL", "FRESHER]"):
        assert leak not in html and leak not in text
    assert "Open to freshers" in text


def test_long_titles_are_clipped():
    subject, html, text = build_alert_email([_job(title="Graduate " + "x" * 400)])
    assert "…" in html and len(subject) < 120


def test_mime_message_has_plain_and_html_parts(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def sendmail(self, frm, to, raw):
            sent.update(frm=frm, to=to, raw=raw)

    monkeypatch.setenv("GMAIL_ADDRESS", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)
    notifier.send_alerts([_job(), _job(title="Trainee", url="https://e.example/2")], "me@example.com")

    msg = message_from_string(sent["raw"])
    assert sent["to"] == "me@example.com"
    assert msg.get_content_type() == "multipart/alternative"
    parts = {p.get_content_type(): p for p in msg.get_payload()}
    assert set(parts) == {"text/plain", "text/html"}
    assert "View job: https://" in parts["text/plain"].get_payload(decode=True).decode("utf-8")
    assert "View job" in parts["text/html"].get_payload(decode=True).decode("utf-8")
    from email.header import decode_header, make_header
    assert str(make_header(decode_header(msg["Subject"]))).startswith("🎯 2 new fresher jobs")


def test_send_alerts_skips_empty(monkeypatch):
    monkeypatch.setattr(notifier, "_send", lambda *a: pytest.fail("should not send"))
    notifier.send_alerts([], "me@example.com")
    notifier.send_alerts([_job()], "")


def test_test_email_renders():
    subject, html, text = build_test_email()
    _parse(html)
    assert "working correctly" in text and "working correctly" in html
