"""Final UI polish + click performance (Oct 2026).

The rules under test:
  * the sidebar holds navigation only: the health / scan status card is gone
    (no leftover space), and that status now lives on Settings (admins), while
    Home's chips and metrics stay as they were
  * the account control's avatar is its own circle — inside the pill with a
    clear margin, evenly spaced from the name, the ADMIN badge and the chevron
    — for every avatar choice, in light and dark (measured in Chromium)
  * clicks reuse the copies they already have: navigation and the account
    menu fetch no shared data and read nothing from Firestore; changing the
    avatar or the display name is ONE write and no read
  * actions are callbacks (one run per click, not draw → act → draw again)
  * loading indicators exist only for real work (slow runs, Refresh, a cold
    start) and respect reduced motion
Firestore is user_store.MemoryBackend; Google, Firebase and GitHub are never
contacted and nothing is emailed."""
import ast
import json
import re
import threading

import pytest

import brand_logos
import config_store
import snapshot_cache
import user_store
from user_store import UserStore
from test_account_onboarding import CountingBackend, JOBS, _ready
from test_firebase_auth import _buttons, admins, google_env, no_env  # noqa: F401  (fixtures)
from test_phase3_user_data import (  # noqa: F401
    A_MAIL, A_UID, B_MAIL, B_UID, AccountIdentity, _sign_in, _use_store,
)
from test_profile_avatar import TRIGGER, _label, _shows, _styles
from test_streamlit_app import NEW_RECORD, OLD_RECORD, REPO, _html, _nav, app  # noqa: F401

SRC = (REPO / "streamlit_app.py").read_text("utf-8")
CSS = SRC[SRC.index('_CSS = """') + len('_CSS = """'):SRC.index('"""', SRC.index('_CSS = """') + 10)]


class WriteCountingBackend(CountingBackend):
    def __init__(self):
        super().__init__()
        self.writes = []

    def set(self, path, data, merge=False):
        self.writes.append((path, json.loads(json.dumps(data))))
        return super().set(path, data, merge=merge)

    def delete(self, path):
        self.writes.append((path, None))
        return super().delete(path)


@pytest.fixture
def ustore(monkeypatch):
    s = UserStore(WriteCountingBackend(), AccountIdentity())
    _use_store(monkeypatch, s)
    return s


def _sidebar_html(at) -> str:
    return "\n".join(e.proto.body for e in at.sidebar.get("html"))


# ── 1. the sidebar holds navigation only ────────────────────────────────────

def test_the_sidebar_status_card_is_gone(app):
    at = app([OLD_RECORD, NEW_RECORD])
    side = _sidebar_html(at)
    assert "Fresher Job Tracker" in side                                        # the brand is still there
    for gone in ("side-foot", "monitored", "Last scan", "All portals healthy", 'role="status"'):
        assert gone not in side
    assert "side_foot_wrap" not in SRC and ".side-foot" not in CSS              # no leftover wrapper or reserved space
    for page in ("jobs", "companies", "monitoring", "email", "settings"):
        _nav(at, page)
        assert "Last scan" not in _sidebar_html(at)


def test_the_member_sidebar_has_no_status_either(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    side = _sidebar_html(at)
    assert "side-foot" not in side and "monitored" not in side and "Last scan" not in side


# ── 2. Settings shows the health and scan status ────────────────────────────

def test_settings_shows_the_tracker_status(app):
    at = app([OLD_RECORD, NEW_RECORD], page="settings")
    html = _html(at)
    status = re.search(r'<dl class="status-grid" role="status">(.*?)</dl>', html, re.S).group(1)
    for label in ("Portal health", "Companies monitored", "Last scan", "Next scheduled"):
        assert f"<dt>{label}</dt>" in status
    n = len(json.loads((at.tmp_path / "companies.json").read_text("utf-8")))
    assert f'<span class="v num">{n}</span>' in status
    assert re.search(r'<span class="pill (healthy|delayed|failing|pending)"><i></i>', status)
    assert "IST" in status and "Tracker status" in html
    at.button(key="btn_set_monitoring").click().run()                            # and a way to the details
    assert not at.exception and at.session_state.page == "monitoring"


def test_settings_status_follows_portal_health(app):
    from test_ui_redesign import NOW, _set_companies
    at = app([])
    _set_companies(at, [{"id": "a", "name": "Alpha", "url": "https://a.example/jobs", "status": "broken",
                         "last_checked": NOW.isoformat(), "status_reason": "HTTP 403"},
                        {"id": "b", "name": "Beta", "url": "https://b.example/jobs", "status": "active",
                         "last_checked": NOW.isoformat()}])
    _nav(at, "settings")
    status = re.search(r'<dl class="status-grid" role="status">(.*?)</dl>', _html(at), re.S).group(1)
    assert '<span class="pill failing"><i></i>1 portal failing</span>' in status
    assert "1 healthy · 1 failing" in status


def test_members_settings_has_no_tracker_internals(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    html = _html(at)
    assert '<h2 class="section-title">Tracker status' not in html and '<dl class="status-grid"' not in html
    assert "btn_set_monitoring" not in _buttons(at)


# ── 3. Home's indicators are unchanged ──────────────────────────────────────

def test_home_keeps_its_health_chips_and_metrics(app):
    at = app([OLD_RECORD, NEW_RECORD])
    html = _html(at)
    assert re.search(r'<span class="chip live (healthy|delayed|failing|pending)"><span class="dot', html)
    assert '<span class="chip num"><span class="ms s16" aria-hidden="true">history</span>Last scan' in html
    assert "Next scheduled" in html
    for stat in ("Active jobs", "New this week", "Companies monitored", "Last scan"):
        assert f'<div class="k">{stat}</div>' in html


# ── 4. the account avatar: its own circle, evenly spaced (real Chromium) ─────

def _tokens(name):
    tree = ast.parse(SRC)
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == name)
    return ast.literal_eval(node.value)


def _account_control(avatar_style: str, theme: str) -> str:
    """The app's CSS and tokens around the exact markup Streamlit 1.65 draws
    for st.popover's trigger (captured from the running app)."""
    css = "\n".join(line for line in CSS.splitlines() if not line.startswith("@import"))
    root = ":root {" + "".join(f"--{k}:{v};" for k, v in _tokens(theme).items()) + "}"
    button = ('<div class="st-key-acct_menu_0"><button kind="secondary" data-testid="stPopoverButton" aria-expanded="false" '
              'style="display:inline-flex;align-items:center;font:inherit;cursor:pointer">'
              '<div style="display:flex;align-items:center"><div><span><span style="display: contents;">'
              '<div data-testid="stMarkdownContainer"><p style="margin:0">Manoj <strong>ADMIN</strong></p></div>'
              '</span></span></div><div aria-hidden="true"><span><span data-testid="stIconMaterial" '
              'style="display:inline-block;width:16px;height:16px;overflow:hidden">expand_more</span></span></div></div>'
              '</button></div>')
    return (f"<html><head><style>{css}\n{root}\n{avatar_style}</style></head>"
            f'<body style="background:var(--bg);padding:40px">{button}</body></html>')


def test_the_account_avatar_sits_cleanly_inside_the_pill_for_every_choice():
    pw = pytest.importorskip("playwright.sync_api")
    trigger = TRIGGER.split("::")[0]
    choices = list(user_store.AVATAR_CHOICES) + [""]
    measured = []
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch()
            pg = browser.new_page()
            for theme in ("LIGHT", "DARK"):
                for key in choices:
                    if key:                      # exactly the rule account_menu() emits for a picked mark
                        style = (f'{TRIGGER} {{ content: ""; background: url("{brand_logos.brand_uri(key)}") center / 58% '
                                 f'no-repeat, {brand_logos.brand_tile(key)}; box-shadow: var(--av-gloss), '
                                 'inset 0 0 0 1px rgba(15,23,42,.10), 0 0 0 2px var(--surface), 0 0 0 3.5px var(--av-ring), var(--av-drop); }')
                    else:
                        style = f'{TRIGGER} {{ content: "M"; }}'
                    pg.set_content(_account_control(style, theme))
                    m = pg.evaluate("""(sel) => {
                      const b = document.querySelector(sel), r = b.getBoundingClientRect(), cs = getComputedStyle(b);
                      const av = getComputedStyle(b, '::before');
                      const p = b.querySelector('p').firstChild, rng = document.createRange(); rng.selectNodeContents(p);
                      const name = rng.getBoundingClientRect(), badge = b.querySelector('strong').getBoundingClientRect();
                      const chev = b.querySelector('[data-testid="stIconMaterial"]').getBoundingClientRect();
                      const padL = parseFloat(cs.paddingLeft), bw = parseFloat(cs.borderLeftWidth), size = parseFloat(av.width);
                      const avLeft = r.left + bw + padL, avTop = r.top + (r.height - parseFloat(av.height)) / 2;
                      return {h: r.height, w: r.width, size, height: parseFloat(av.height), radius: av.borderRadius,
                              bg: av.backgroundImage, shadow: av.boxShadow,
                              clearLeft: avLeft - r.left - bw, clearTop: avTop - r.top - bw,
                              gapName: name.left - (avLeft + size), gapBadge: badge.left - name.right,
                              gapChevron: chev.left - badge.right, chevRight: r.right - chev.right,
                              font: getComputedStyle(b.querySelector('p')).fontFamily};
                    }""", trigger)
                    measured.append((theme, key or "initials", m))
            browser.close()
    except pw.Error as e:                                     # no browser installed here
        pytest.skip(f"chromium unavailable: {e}")
    assert len(measured) == 2 * len(choices)
    for theme, key, m in measured:
        ctx = (theme, key, m)
        assert m["size"] == m["height"] == 34 and m["radius"] == "50%", ctx                 # a true circle
        ring = 3.5                                                                         # gap ring + accent ring
        assert m["clearLeft"] - ring >= 3 and m["clearTop"] - ring >= 3, ctx               # never touches the pill's edge
        assert abs(m["clearTop"] - (m["h"] - 2 - m["height"]) / 2) < 0.6, ctx              # vertically centred
        assert "0px 0px 0px 3.5px" in m["shadow"].replace(" 3.5px 0px", " 3.5px"), ctx     # the accent ring is drawn
        assert m["gapName"] >= 10 and 6 <= m["gapBadge"] <= 12, ctx                       # even spacing ...
        assert m["gapChevron"] >= 6 and m["chevRight"] >= 10, ctx                         # ... up to the chevron
        assert "Plus Jakarta Sans" in m["font"], ctx                                     # the app's typography
        if key != "initials":
            assert brand_logos.brand_uri(key)[:40] in m["bg"].replace('"', ""), ctx       # the chosen mark, not initials


# ── 5–7. avatars, display name, roles ───────────────────────────────────────

def test_every_avatar_choice_is_one_write_and_no_read(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    for key in list(user_store.AVATAR_CHOICES) + [""]:
        ustore.backend.reads, ustore.backend.writes = 0, []
        at.button(key=f"av_pick_{key or 'initials'}").click().run()
        assert not at.exception
        assert ustore.backend.writes == [(f"users/{A_UID}", {"avatar": key})], key        # exactly the one field
        assert ustore.backend.reads == 0, key                                           # nothing read back
        assert _shows(at, key), key                                                     # header, menu and Account page
        assert at.session_state["_pcache"]["profile"]["avatar"] == key
    assert user_store.avatar_from(ustore.backend.get(f"users/{A_UID}")) == ""


def test_a_display_name_save_is_one_write_and_no_read(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    before = dict(ustore.backend.get(f"users/{A_UID}"))
    ustore.backend.reads, ustore.backend.writes = 0, []
    at.text_input(key="display_name_input").set_value("  Priya Sharma ")
    at.button(key="btn_save_display_name").click().run()
    assert not at.exception
    assert ustore.backend.writes == [(f"users/{A_UID}", {"preferred_name": "Priya Sharma"})]
    assert ustore.backend.reads == 0
    assert _label(at).startswith("Priya") and "Priya Sharma" in _html(at)
    after = ustore.backend.get(f"users/{A_UID}")
    assert {k: v for k, v in after.items() if k != "preferred_name"} == {k: v for k, v in before.items() if k != "preferred_name"}
    ustore.backend.reads = 0                                                           # (the check above read it)
    _nav(at, "home")                                                                   # and it stays without a re-read
    assert ustore.backend.reads == 0 and _label(at).startswith("Priya")


def test_admin_badge_for_admins_only(app, google_env, ustore, admins):
    admins(A_MAIL)
    _ready(ustore, A_UID, A_MAIL)
    _ready(ustore, B_UID, B_MAIL)
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    assert "**ADMIN**" in _label(a)
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS)
    assert "ADMIN" not in _label(b)


def test_an_avatar_change_never_touches_another_account(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    _ready(ustore, B_UID, B_MAIL).set_avatar("apple")
    b_before = dict(ustore.backend.get(f"users/{B_UID}"))
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    a.button(key="av_pick_amazon").click().run()
    assert ustore.backend.get(f"users/{B_UID}") == b_before
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS, page="settings")
    assert _shows(b, "apple") and not _shows(b, "amazon")


# ── 8–9. clicks reuse what they have ────────────────────────────────────────

def test_navigation_menu_and_settings_clicks_fetch_and_read_nothing(app, google_env, ustore, admins, monkeypatch):
    calls = []
    catalogue = json.loads((REPO / "data" / "companies.json").read_text("utf-8"))

    def fake_fetch(paths, *a, **k):
        calls.append(list(paths))
        return {"data/companies.json": catalogue, "data/settings.json": {"recipient_email": "me@example.com"},
                "data/seen_jobs.json": list(JOBS)}
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "fetch_remote_json", fake_fetch)
    admins(A_MAIL)
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    assert len(calls) == 1
    ustore.backend.reads, ustore.backend.writes = 0, []
    lookups = ustore.identity.calls[:]
    for key in ("nav_jobs", "nav_companies", "nav_monitoring", "nav_email", "nav_settings", "btn_set_monitoring",
                "nav_settings", "acct_settings", "acct_preferences", "acct_companies", "acct_alerts", "nav_home"):
        at.button(key=key).click().run()
        assert not at.exception, key
    assert len(calls) == 1                                       # no shared-data fetch
    assert ustore.backend.reads == 0 and ustore.backend.writes == []    # no Firestore read or write
    assert ustore.identity.calls == lookups                      # no Auth lookup


def test_a_dismissal_is_written_once_and_not_read_back(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="jobs")
    dismiss = next(k for k in _buttons(at) if k.startswith("dismiss_"))
    ustore.backend.reads, ustore.backend.writes = 0, []
    at.button(key=dismiss).click().run()
    assert not at.exception
    assert len(ustore.backend.writes) == 1 and ustore.backend.writes[0][0].startswith(f"users/{A_UID}/dismissed/")
    assert ustore.backend.reads == 0
    assert dismiss not in _buttons(at)                           # gone from the list at once
    assert ustore.for_uid(A_UID).dismissed() == at.session_state["_pcache"]["dismissed"]


def test_following_a_company_is_written_once_and_not_read_back(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    cid = json.loads((at.tmp_path / "companies.json").read_text("utf-8"))[0]["id"]
    at.session_state["page"], at.session_state["company_id"] = "companies", cid
    at.run()
    ustore.backend.reads, ustore.backend.writes = 0, []
    at.button(key="btn_follow").click().run()
    assert not at.exception
    assert len(ustore.backend.writes) == 1 and ustore.backend.reads == 0
    assert at.session_state["_pcache"]["watchlist"] == ustore.for_uid(A_UID).watchlist()


def test_preferences_save_is_one_write_and_not_read_back(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="preferences")
    next(b for b in at.button_group if b.key == "pref_locations").set_value(["Pune"])
    ustore.backend.reads, ustore.backend.writes = 0, []
    at.button(key="btn_prefs_save").click().run()
    assert not at.exception
    assert [p for p, _ in ustore.backend.writes] == [f"users/{A_UID}"] and ustore.backend.reads == 0
    saved = ustore.backend.get(f"users/{A_UID}")["preferences"]
    assert saved["locations"] == ["Pune"] and at.session_state["_pcache"]["profile"]["preferences"] == saved


def test_a_failed_write_falls_back_to_a_full_reread(app, google_env, ustore, monkeypatch):
    """When a save fails, nothing is patched in: the next run reads the truth."""
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")

    def boom(*a, **k):
        raise user_store.UserStoreError("unavailable", "unavailable")
    monkeypatch.setattr(user_store.UserData, "set_avatar", boom)
    at.button(key="av_pick_ibm").click().run()
    assert not at.exception
    assert user_store.avatar_from(at.session_state["_pcache"]["profile"]) == ""
    assert "save your avatar right now" in _html(at)


# ── 10. actions are callbacks; loading only for real work ───────────────────

def test_actions_run_once_per_click():
    """Handled inline, these drew the page up to the button, acted, then
    called st.rerun() and drew everything again."""
    for inline in ('if st.button("Refresh"', 'if st.button("Run check"', 'if st.button("Dismiss"',
                   'elif st.button("Dismiss"', 'if st.button("Restore"', 'if st.button("Reload data"',
                   'if st.button("Previous"', 'if st.button("Next"', 'if st.button("Use light theme"',
                   'if st.form_submit_button("Save preferences"', 'if st.form_submit_button("Save", key="btn_my_alerts"'):
        assert inline not in SRC, inline
    for key, cb in (("btn_refresh", "_refresh_click"), ("btn_run_check", "_run_check_click"), ("btn_reload", "_refresh_click"),
                    ("btn_theme", "_toggle_theme"), ("btn_prefs_save", "_save_preferences_click")):
        assert re.search(rf'key="{key}".{{0,240}}?on_click={cb}', SRC, re.S), key


def test_refresh_and_run_check_still_do_their_work(app, monkeypatch):
    from test_security import _fake_github
    fetched, dispatched = [], []
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "fetch_remote_json", lambda paths, *a, **k: fetched.append(1) or {})
    _fake_github(monkeypatch, dispatched)
    at = app([OLD_RECORD, NEW_RECORD])
    n = len(fetched)
    at.button(key="btn_refresh").click().run()
    assert len(fetched) == n + 1 and "Showing the latest data" in _html(at)          # Refresh reloads now
    at.button(key="btn_run_check").click().run()
    assert dispatched == ["main"] and "Check started" in _html(at)                    # Run check dispatches
    assert at.session_state["last_check_trigger"] > 0                                  # portals show "Checking"


def test_loading_indicators_are_only_for_real_work(app):
    # the top bar belongs to a running script and waits before it shows, so quick clicks never flash it
    bar = re.search(r'\.stApp\[data-test-script-state="running"\]:has\(\.jt-skel\)::after \{ animation: jt-busy-in 220ms ease-out (\d+)ms both', CSS)
    assert bar and int(bar.group(1)) >= 400                                              # (first loads only)
    top = re.search(r'\[data-testid="stMain"\]::before \{ visibility: visible; transition: visibility 0s linear (\d+)ms', CSS)
    assert top and int(top.group(1)) >= 400                                              # the bar of any other run
    assert re.search(r"\.stApp::after \{[^}]*opacity: 0;", CSS)                          # hidden otherwise
    # the pressed button: Refresh spins at once, others breathe only after a pause
    assert re.search(r"jt-busy-pulse 1s ease-in-out (\d+)ms infinite", CSS)
    # reduced motion: no sliding, spinning or pulsing — a still bar
    reduced = CSS[CSS.index("/* the bar appears (still after the pause)"):]
    reduced = reduced[:reduced.index("}\n}") + 3]
    assert "animation: none !important" in reduced and "jt-busy-slide" not in reduced
    # the cold-start skeleton is replaced as soon as the data is there
    at = app([OLD_RECORD, NEW_RECORD])
    assert '<div class="jt-skel"' not in _html(at)
    _nav(at, "jobs")
    assert '<div class="jt-skel"' not in _html(at)


def test_snapshot_cache_knows_when_it_has_data():
    c = snapshot_cache.SnapshotCache(lambda: {"a.json": [1]}, spawn=lambda fn: fn())
    assert not c.has_data()
    assert c.get() == {"a.json": [1]} and c.has_data()


# ── GitHub: fewer round trips ───────────────────────────────────────────────

def test_shared_files_are_read_side_by_side_on_lazy_handles(monkeypatch):
    import github
    made, threads = [], set()

    class Contents:
        def __init__(self, path):
            self.decoded_content = json.dumps({"file": path}).encode()

    class Repo:
        def get_contents(self, path):
            threads.add(threading.get_ident())
            return Contents(path)

    class FakeGithub:
        def __init__(self, token, **kw):
            made.append(kw)

        def get_repo(self, name, **kw):
            return Repo()
    monkeypatch.setattr(github, "Github", FakeGithub)
    out = config_store.fetch_remote_json(["a.json", "b.json", "c.json"], token="t")
    assert out == {p: {"file": p} for p in ("a.json", "b.json", "c.json")}
    assert made and all(kw.get("lazy") is True for kw in made)            # no request for the repository itself
    assert len(threads) >= 1 and threading.get_ident() not in threads       # read off the caller's thread


def test_a_file_that_fails_keeps_the_last_good_copy(monkeypatch):
    class Repo:
        def get_contents(self, path):
            if path == "bad.json":
                raise RuntimeError("boom")
            class C:
                decoded_content = b"null" if path == "null.json" else b"[1]"
            return C()
    monkeypatch.setattr(config_store, "_get_repo", lambda token: Repo())
    assert config_store.fetch_remote_json(["ok.json", "bad.json", "null.json"], token="t") == {"ok.json": [1]}
