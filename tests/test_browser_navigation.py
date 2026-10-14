"""A real browser: every navigation opens the destination at its top, and
same-page actions keep the place.

The app runs with ``streamlit run`` from a temporary copy of the repository
(no secrets, so no sign-in; no GitHub token, so nothing is fetched or saved
remotely) and Chromium drives it. Skipped where Chromium isn't installed."""
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from test_streamlit_app import NEW_RECORD, REPO

PAGES = ["home", "jobs", "companies", "monitoring", "email", "settings"]
SCROLL_TOP = "() => { const m = document.querySelector('[data-testid=\"stMain\"]'); return Math.round(m.scrollTop); }"
TO_BOTTOM = "() => { const m = document.querySelector('[data-testid=\"stMain\"]'); m.scrollTo(0, m.scrollHeight); return m.scrollTop; }"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    pytest.importorskip("playwright.sync_api")
    root = tmp_path_factory.mktemp("app")
    # the repository's layout: the app at the root, modules in src/, data in data/
    shutil.copy(REPO / "streamlit_app.py", root / "streamlit_app.py")
    shutil.copytree(REPO / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "data").mkdir()
    for name in ("companies.json", "settings.json", "logos.json"):
        shutil.copy(REPO / "data" / name, root / "data" / name)
    shutil.copytree(REPO / "assets", root / "assets")
    (root / ".streamlit").mkdir()
    shutil.copy(REPO / ".streamlit" / "config.toml", root / ".streamlit" / "config.toml")
    many = [dict(NEW_RECORD, id=f"nav_{i}", url=f"https://jobs.example/nav/{i}", title=f"Graduate Engineer {i}")
            for i in range(40)]
    (root / "data" / "seen_jobs.json").write_text(json.dumps(many), "utf-8")
    port = _free_port()
    env = {k: v for k, v in __import__("os").environ.items()
           if not k.startswith(("GITHUB_", "GMAIL_", "FIREBASE_", "JT_", "ALERT_"))}
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "streamlit_app.py", "--server.port", str(port),
                             "--server.headless", "true", "--browser.gatherUsageStats", "false"],
                            cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/"
    for _ in range(120):
        try:
            urllib.request.urlopen(url + "_stcore/health", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.skip("streamlit didn't start")
    yield url
    proc.kill()


def _idle(pg):
    pg.wait_for_function("""() => { const a = document.querySelector('[data-testid="stApp"]');
        return a && a.getAttribute('data-test-script-state') === 'notRunning'; }""", timeout=60000)


def _click(pg, selector):
    pg.locator(selector).first.click()
    try:
        pg.wait_for_function("""() => document.querySelector('[data-testid="stApp"]').getAttribute('data-test-script-state') === 'running'""",
                             timeout=2000)
    except Exception:
        pass
    _idle(pg)
    pg.wait_for_timeout(300)


def test_navigation_opens_every_page_at_its_top_and_same_page_actions_keep_the_place(server):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except pw.Error as e:                                 # no browser installed here
            pytest.skip(f"chromium unavailable: {e}")
        if True:
            pg = browser.new_page(viewport={"width": 1280, "height": 800})
            pg.goto(server + "?page=jobs")
            pg.wait_for_selector(".st-key-nav_home", timeout=90000)
            _idle(pg)
            pg.wait_for_timeout(800)
            results = {}
            for dest in PAGES + ["jobs"]:                      # down a long page, then away (and back to Jobs)
                assert pg.evaluate(TO_BOTTOM) > 200
                pg.wait_for_timeout(200)
                _click(pg, f".st-key-nav_{dest} button")
                results[dest] = pg.evaluate(SCROLL_TOP)
            # same page: sorting and paging keep the place
            pg.evaluate("() => document.querySelector('[data-testid=\"stMain\"]').scrollTo(0, 100)")
            pg.wait_for_timeout(200)
            before_sort = pg.evaluate(SCROLL_TOP)        # scrolled, with the sort box still in view
            box = pg.locator(".st-key-j_sort input").first
            box.click()
            pg.wait_for_timeout(300)
            if not pg.locator("[role='option']").count():        # a narrow combobox opens from the keyboard
                box.press("ArrowDown")
                pg.wait_for_timeout(300)
            _click(pg, "[role='option']:has-text('Title A')")
            after_sort = pg.evaluate(SCROLL_TOP)
            pg.evaluate(TO_BOTTOM)
            pg.wait_for_timeout(200)
            before_next = pg.evaluate(SCROLL_TOP)
            _click(pg, ".st-key-alerts_next button")
            after_next = pg.evaluate(SCROLL_TOP)
            sort_value = pg.locator(".st-key-j_sort input").first.input_value()
            # the account-style menu isn't here (no sign-in), but a job's own page is a new destination
            pg.evaluate(TO_BOTTOM)
            _click(pg, '[class*="st-key-crit_"] button')
            detail_top = pg.evaluate(SCROLL_TOP)
            browser.close()
    assert results == {d: 0 for d in PAGES + ["jobs"]}, results
    assert before_sort > 0 and abs(after_sort - before_sort) <= 2, (before_sort, after_sort)
    assert after_next >= before_next - 2 and after_next > 0, (before_next, after_next)
    assert sort_value == "Title A–Z"
    assert detail_top == 0
