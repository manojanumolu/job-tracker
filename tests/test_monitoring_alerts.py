"""Monitoring-failure emails after 3 consecutive scheduled scans, once per
incident, with a recovery note; job alerts untouched. No network, no email."""
from datetime import datetime, timezone

import alerts
import monitoring_alerts as ma

NOW = datetime(2026, 11, 20, 6, 10, tzinfo=timezone.utc)


def co(cid, status="failing", blocked=3, shared=True, reason="HTTP 429 from the Workday jobs API", **scan):
    c = {"id": cid, "name": cid.title(), "status": status, "status_reason": reason if status != "active" else "",
         "source": "Workday API", "last_success": "2026-11-16T06:04:00+00:00", "last_checked": "2026-11-20T06:05:00+00:00",
         "scan": {"blocked_scans": blocked, "blocked_since": "2026-11-19T12:03:00+00:00",
                  "last_failed_at": "2026-11-20T06:05:00+00:00", **scan}}
    if status == "active":
        c["scan"] = {}
    if not shared:
        c["shared_alerts"] = False
    return c


class Mail:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, to, subject, html, text):
        if self.fail:
            raise OSError("smtp down")
        self.sent.append((to, subject, html, text))


def test_two_failed_scans_send_nothing_the_third_warns_once():
    mail = Mail()
    assert ma.run([co("nvidia", blocked=2)], {"open": {}}, "admin@example.com", mail, NOW) is None
    assert mail.sent == []
    ledger = ma.run([co("nvidia", blocked=3)], {"open": {}}, "admin@example.com", mail, NOW)
    assert list(ledger["open"]) == ["nvidia"] and len(mail.sent) == 1
    to, subject, html, text = mail.sent[0]
    assert to == "admin@example.com" and "Nvidia" in subject
    for needed in ("Workday API", "rate-limited", "HTTP 429", "First failure: 19 Nov 2026 12:03 UTC",
                   "Latest failure: 20 Nov 2026 06:05 UTC", "Last successful scan: 16 Nov 2026 06:04 UTC"):
        assert needed in text
    # the same incident on the 4th, 5th ... scan: never re-sent
    assert ma.run([co("nvidia", blocked=4)], ledger, "admin@example.com", mail, NOW) is None
    assert ma.run([co("nvidia", blocked=9)], ledger, "admin@example.com", mail, NOW) is None
    assert len(mail.sent) == 1


def test_recovery_is_sent_once_and_closes_the_incident():
    mail = Mail()
    ledger = ma.run([co("cisco")], {"open": {}}, "a@example.com", mail, NOW)
    ledger = ma.run([co("cisco", status="active")], ledger, "a@example.com", mail, NOW)
    assert ledger == {"open": {}} and "recovered" in mail.sent[-1][1].lower() and len(mail.sent) == 2
    assert ma.run([co("cisco", status="active")], ledger, "a@example.com", mail, NOW) is None
    # failing again later is a NEW incident (after 3 more scheduled scans)
    assert ma.run([co("cisco", blocked=1)], ledger, "a@example.com", mail, NOW) is None
    assert ma.run([co("cisco", blocked=3)], ledger, "a@example.com", mail, NOW)["open"]
    assert len(mail.sent) == 3


def test_a_failed_send_keeps_the_ledger_so_the_next_scan_retries():
    assert ma.run([co("amgen")], {"open": {}}, "a@example.com", Mail(fail=True), NOW) is None
    mail = Mail()
    assert ma.run([co("amgen")], {"open": {}}, "a@example.com", mail, NOW)["open"] and len(mail.sent) == 1


def test_one_digest_for_many_companies_and_categories():
    cos = [co("a"), co("b", reason="the career-site sitemap is incomplete or not a sitemap"),
           co("c", reason="Workday listing response failed validation: invalid total"),
           co("d", reason="HTTP 503 from the jobs API")]
    mail = Mail()
    ma.run(cos, {"open": {}}, "x@example.com", mail, NOW)
    text = mail.sent[0][3]
    assert len(mail.sent) == 1 and "4 companies" in mail.sent[0][1]
    for cat in ("rate-limited", "failing to parse", "returning invalid data", "inaccessible"):
        assert f"Problem: {cat}" in text


def test_another_admins_own_companies_are_never_reported_to_the_primary_admin():
    mail = Mail()
    cos = [co("sanofi", shared=False, blocked=12), co("pwc", shared=False, blocked=12)]
    assert ma.run(cos, {"open": {}}, "primary@example.com", mail, NOW) is None and mail.sent == []


def test_healthy_and_valid_empty_companies_are_never_failures():
    mail = Mail()
    assert ma.run([co("swiggy", status="active")], {"open": {}}, "a@example.com", mail, NOW) is None
    assert mail.sent == []


def test_no_recipient_sends_nothing_and_keeps_the_incident_pending():
    assert ma.run([co("x")], {"open": {}}, "", Mail(), NOW) is None


def test_publish_step_wires_it_without_touching_job_alerts(monkeypatch, tmp_path):
    published, mail = [], Mail()
    monkeypatch.setattr("config_store.DATA", tmp_path)
    alerts._monitoring([co("marvell")], "a@example.com", lambda m, f: published.append((m, f)) or True, NOW, send=mail)
    assert published[0][0] == alerts.MONITORING_MESSAGE and "marvell" in published[0][1][ma.LEDGER]["open"]
    assert len(mail.sent) == 1
    # a broken ledger file never stops the run
    (tmp_path / "monitoring_incidents.json").write_text("{not json")
    alerts._monitoring([co("marvell")], "a@example.com", lambda m, f: True, NOW, send=mail)


# ── end to end through the real publish step (alerts.run), SMTP stubbed ──

def test_end_to_end_three_scheduled_failures_one_warning_then_recovery_and_job_alerts_unaffected(tmp_path, monkeypatch):
    import json
    import config_store
    import notifier
    from test_alerts import FakeRemote, Mailer, _rec
    monkeypatch.setenv("ALERT_RECIPIENT", "me@example.com")
    for name, attr in (("seen_jobs.json", "SEEN_FILE"), ("companies.json", "COMPANIES_FILE"), ("settings.json", "SETTINGS_FILE")):
        monkeypatch.setattr(config_store, attr, tmp_path / name)
    monkeypatch.setattr(config_store, "DATA", tmp_path)
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "companies.json")
    monitor_mail = Mail()
    monkeypatch.setattr(notifier, "_send", monitor_mail)

    class Remote(FakeRemote):                     # also keeps the incident ledger, as repo_sync.publish does
        def __call__(self, message, local):
            if alerts.MONITORING_MESSAGE == message:
                self.calls.append(message)
                (tmp_path / "monitoring_incidents.json").write_text(json.dumps(local[ma.LEDGER]))
                return True
            return super().__call__(message, local)

    remote, job_mail = Remote(), Mailer()
    (tmp_path / "settings.json").write_text("{}")
    job = _rec("Graduate Engineer Trainee", "https://ok.example/job/1")
    (tmp_path / "seen_jobs.json").write_text(json.dumps([job]))
    for n in (1, 2, 3, 4, 5):                       # five scheduled scans of a blocked company
        (tmp_path / "companies.json").write_text(json.dumps([co("nvidia", blocked=n)]))
        assert alerts.run(send=job_mail, publisher=remote) == 0
        assert len(monitor_mail.sent) == (0 if n < 3 else 1), n      # nothing at 1-2, one at 3, no repeat at 4-5
    assert job_mail.sent == [["Graduate Engineer Trainee"]]        # the job alert went out once, unchanged
    (tmp_path / "companies.json").write_text(json.dumps([co("nvidia", status="active")]))
    assert alerts.run(send=job_mail, publisher=remote) == 0
    assert len(monitor_mail.sent) == 2 and "recovered" in monitor_mail.sent[1][1].lower()
    assert json.loads((tmp_path / "monitoring_incidents.json").read_text()) == {"open": {}}
    assert alerts.run(send=job_mail, publisher=remote) == 0
    assert len(monitor_mail.sent) == 2 and job_mail.sent == [["Graduate Engineer Trainee"]]


def test_first_failure_is_the_real_start_and_the_count_says_when_it_began():
    c = co("hitachi")
    c["scan"]["failing_since"] = "2026-11-16T09:05:00+00:00"            # failing for days before counting began
    mail = Mail()
    ma.run([c], {"open": {}}, "a@example.com", mail, NOW)
    text = mail.sent[0][3]
    assert "First failure: 16 Nov 2026 09:05 UTC" in text
    assert "Consecutive scheduled scans: 3 (counted since 19 Nov 2026 12:03 UTC)" in text
