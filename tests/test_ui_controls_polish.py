"""Oct 2026 UI polish: selection controls, search boxes, type, the hero.

Bugs this pins (found in Chromium against the real app):
* Streamlit 1.65 draws select menus as portals on the page body, outside
  .stApp - they rendered in Source Sans on a bare panel; the BaseWeb-era
  rules matched nothing.
* A multiselect's chips scrolled sideways ("Bengaluru" cut to "ru x").
* A multiselect's menu stays open after each pick while the app reruns: a
  stale menu overlapped the next control, and Tab out of the field selected
  the highlighted option.
* A literal "<body>" in a CSS comment made Streamlit drop the whole
  stylesheet (every page fell back to default fonts and chrome).
The browser half runs a small page with the app's own _CSS and _MENU_GUARD
and multiselects configured exactly like the Jobs filters.
"""
import ast
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "streamlit_app.py").read_text("utf-8")
CONSTS = {t.targets[0].id: t.value.value for t in ast.parse(SRC).body
          if isinstance(t, ast.Assign) and len(t.targets) == 1 and isinstance(t.targets[0], ast.Name)
          and t.targets[0].id in ("_CSS", "_MENU_GUARD") and isinstance(t.value, ast.Constant)}
CSS = CONSTS["_CSS"]
GUARD = CONSTS["_MENU_GUARD"]
MENUS = ':is([data-testid="stSelectboxVirtualDropdown"], [data-testid="stMultiSelectDropdown"])'


# ── static contract ──────────────────────────────────────────────────────────

def test_the_stylesheet_holds_no_markup():
    """st.html parses its body as HTML: one "<tag>" inside the CSS (even in a
    comment) and the whole <style> was dropped."""
    assert "<" not in CSS


def test_menus_get_the_app_type_and_surface():
    assert f"{MENUS},\n{MENUS} *:not(.ms):not([data-testid=\"stIconMaterial\"]) {{\n  font-family: var(--font) !important;" in CSS
    assert f"{MENUS} {{ background: var(--raised) !important;" in CSS
    assert f'{MENUS} [role="option"][aria-selected="true"]::after {{' in CSS and 'content: "check"' in CSS
    assert "body { font-family: var(--font); }" in CSS                   # anything else on the page body


def test_chips_wrap_and_fields_match_the_search_box():
    assert '[data-testid="stMultiSelectTagsContainer"] { flex-wrap: wrap !important; overflow: visible !important;' in CSS
    assert ('[class*="st-key-filters_"] :is(.stSelectbox, .stMultiSelect) div[role="group"] { border-radius: var(--r) !important; '
            'min-height: 44px !important;') in CSS
    assert "border-radius: 999px !important; min-height: 36px" not in CSS          # no more pill-shaped selects


def test_every_jobs_multiselect_wraps_and_has_no_select_all():
    calls = re.findall(r"st\.multiselect\((.*?)\)\n", SRC, re.S)
    assert len(calls) == 4
    for call in calls:
        assert "wrap=True" in call and "select_all=False" in call, call


def test_every_search_box_gets_the_magnifying_glass():
    assert ('.stTextInput:has(input[aria-label^="Search"])) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])::before {\n'
            '  content: "search";') in CSS
    for label in re.findall(r'st\.text_input\("(Search[^"]*)"', SRC):
        assert label.startswith("Search")


def test_the_menu_guard_is_installed_once_hidden_and_scoped_to_select_fields():
    assert "if (window.__jtMenuGuard) return; window.__jtMenuGuard = true;" in GUARD
    assert GUARD.count("document.addEventListener(") == 3 and GUARD.count("}, true);") == 3     # all capture phase
    assert "'[data-testid=\"stMultiSelect\"], [data-testid=\"stSelectbox\"]'" in GUARD
    assert 'with st.container(key="jt_menu_guard"):\n    st.html(_MENU_GUARD, unsafe_allow_javascript=True)' in SRC
    assert ":is(.st-key-jt_scroll_top, .st-key-jt_menu_guard, .st-key-jt_auth_cookie)" in CSS and "display: none !important" in CSS


def test_preference_and_onboarding_pills_share_the_polished_pill_style():
    pills = ':is([class*="st-key-tl_pick_"], [class*="st-key-pref_"], [class*="st-key-ob_"]) button[data-variant="pills"]'
    assert f"{pills}:is([aria-checked=\"true\"], [aria-pressed=\"true\"], [data-selected=\"true\"])" in CSS


def test_hero_feed_is_compositor_only_and_still_under_reduced_motion():
    for name in ("jt-feed-type", "jt-feed-row", "jt-feed-ok", "jt-feed-seal"):
        body = re.search(rf"@keyframes {name} \{{(.*?)\}}\s*\}}", CSS, re.S).group(1)
        assert set(re.findall(r"([a-z-]+)\s*:", body)) <= {"opacity", "transform"}, name
    rm = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
    assert (".feed-q .qbar i, .feed-row, .feed-row .ok, .feed-seal, .opps > .glow-bg { animation: none !important; "
            "opacity: 1 !important; transform: none !important; }") in rm
    assert ".feed-row:hover" in CSS and ".feed-row:hover .ic { transform: scale(1.06); }" in CSS


# ── in a real browser ────────────────────────────────────────────────────────

MINI = '''import ast, pathlib
import streamlit as st
st.set_page_config(layout="wide")                      # as the app
src = pathlib.Path(r"{repo}").read_text("utf-8")
c = {{t.targets[0].id: t.value.value for t in ast.parse(src).body if isinstance(t, ast.Assign) and len(t.targets) == 1
      and isinstance(t.targets[0], ast.Name) and t.targets[0].id in ("_CSS", "_MENU_GUARD")}}
st.html("<style>" + c["_CSS"] + "</style>")
with st.container(key="jt_menu_guard"):
    st.html(c["_MENU_GUARD"], unsafe_allow_javascript=True)
with st.container(key="menu_test"):
    a, b, s = st.columns(3)
    with a:
        loc = st.multiselect("Location", ["Bengaluru", "Hyderabad", "Pune", "Chennai", "Mumbai", "Delhi NCR", "Kolkata"],
                             key="t_ploc", placeholder="Any location", wrap=True, select_all=False)
    with b:
        fam = st.multiselect("Job family", ["Software engineering", "Data & analytics", "Cloud & DevOps"],
                             key="t_pfam", placeholder="Any family", wrap=True, select_all=False)
    with s:
        st.selectbox("Sort", ["Newest first", "Oldest first"], key="t_sort")
st.markdown("picked: " + ", ".join(loc) + " | " + ", ".join(fam))
'''

OPEN = """() => [...document.querySelectorAll('[role="listbox"]')].filter(e => {
  const d = e.closest('[data-testid]') || e; const r = d.getBoundingClientRect(); let o = 1, n = d;
  while (n && n !== document.body) { o *= +getComputedStyle(n).opacity; n = n.parentElement; }
  return r.height > 0 && o > 0.05; }).map(e => (e.closest('[data-testid]') || e).getAttribute('data-testid'))"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    pw = pytest.importorskip("playwright.sync_api")
    root = tmp_path_factory.mktemp("menus")
    (root / "mini.py").write_text(MINI.format(repo=REPO / "streamlit_app.py"), "utf-8")
    (root / ".streamlit").mkdir()
    shutil.copy(REPO / ".streamlit" / "config.toml", root / ".streamlit" / "config.toml")
    port = _free_port()
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith(("GITHUB_", "GMAIL_", "FIREBASE_", "JT_"))}
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "mini.py", "--server.port", str(port),
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
    try:
        with pw.sync_playwright() as p:
            try:
                br = p.chromium.launch()
            except Exception as e:                                   # no Chromium here
                pytest.skip(f"chromium unavailable: {e}")
            yield url, br
            br.close()
    finally:
        proc.kill()


def _page(mini):
    url, br = mini
    pg = br.new_page(viewport={"width": 1100, "height": 800})
    pg.goto(url)
    pg.wait_for_selector('[data-testid="stMultiSelect"]', timeout=60000)
    _settle(pg)
    return pg


def _settle(pg):
    pg.wait_for_function("""() => document.querySelector('[data-testid="stApp"]').getAttribute('data-test-script-state') === 'notRunning'""",
                         timeout=60000)
    pg.wait_for_timeout(700)


def _pick_two_fast(pg):
    """Open Location and pick two options at human speed (the second while
    the app is still rerunning from the first)."""
    pg.locator('[data-testid="stMultiSelect"]').nth(0).locator("input").click()
    pg.wait_for_timeout(250)
    pg.locator('[role="option"]').nth(0).click()
    pg.wait_for_timeout(120)
    pg.locator('[role="option"]').nth(1).click()
    pg.wait_for_timeout(100)


def _chips(pg, n=0):
    return pg.locator('[data-testid="stMultiSelect"]').nth(n).locator('[aria-label="Selected values"] > span').count()


def test_clicking_outside_closes_the_menu_after_picking_two(mini):
    pg = _page(mini)
    _pick_two_fast(pg)
    pg.mouse.click(1000, 700)
    pg.wait_for_timeout(400)
    assert pg.evaluate(OPEN) == []                     # closed at once, not after the rerun
    _settle(pg)
    assert pg.evaluate(OPEN) == [] and _chips(pg) == 2
    assert pg.inner_text('[data-testid="stMain"]').count(",") >= 1          # the app received both picks
    pg.close()


def test_opening_another_filter_leaves_exactly_that_menu_open(mini):
    pg = _page(mini)
    _pick_two_fast(pg)
    box = pg.locator('[data-testid="stMultiSelect"]').nth(1).locator('div[role="group"]').first.bounding_box()
    pg.mouse.click(box["x"] + 40, box["y"] + box["height"] / 2)              # where a person clicks, mid-rerun
    pg.wait_for_timeout(400)
    assert pg.evaluate(OPEN) == ["stMultiSelectDropdown"]
    _settle(pg)
    assert pg.evaluate(OPEN) == ["stMultiSelectDropdown"]
    assert pg.evaluate("() => document.activeElement.getAttribute('aria-label')") == "Job family"
    assert _chips(pg, 0) == 2 and _chips(pg, 1) == 0                         # the click picked nothing by accident
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)
    pg.locator('[data-testid="stSelectbox"]').locator("input").click()     # and on to the sort menu
    pg.wait_for_timeout(400)
    assert pg.evaluate(OPEN) == ["stSelectboxVirtualDropdown"]
    pg.close()


def test_tab_moves_on_without_selecting_the_highlighted_option(mini):
    pg = _page(mini)
    _pick_two_fast(pg)
    pg.keyboard.press("Tab")
    _settle(pg)
    assert _chips(pg) == 2 and pg.evaluate(OPEN) == []
    pg.close()


def test_chips_wrap_and_the_whole_field_opens_and_clears(mini):
    pg = _page(mini)
    field = pg.locator('[data-testid="stMultiSelect"]').nth(0)
    field.locator("input").click()
    pg.wait_for_timeout(250)
    for _ in range(6):
        pg.locator('[role="option"]').nth(0).click()
        _settle(pg)
    pg.mouse.click(1000, 700)
    _settle(pg)
    tags = field.locator('[data-testid="stMultiSelectTagsContainer"]')
    assert tags.evaluate("e => e.scrollWidth <= e.clientWidth + 1")          # nothing hidden sideways
    group = field.locator('div[role="group"]').first
    box = group.bounding_box()
    tops = sorted({round(b["y"]) for b in (c.bounding_box() for c in field.locator('[aria-label="Selected values"] > span').all())})
    assert len(tops) >= 2                                                     # wrapped onto a second row
    chip = field.locator('[aria-label="Selected values"] > span > span').first.bounding_box()
    pg.mouse.click(chip["x"] + 8, chip["y"] + chip["height"] / 2)             # clicking a chip's label opens the menu
    pg.wait_for_timeout(500)
    assert pg.evaluate(OPEN) == ["stMultiSelectDropdown"] and _chips(pg) == 6
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)
    field.locator('button[aria-label="Clear all"]').click()
    _settle(pg)
    assert _chips(pg) == 0 and group.bounding_box()["height"] < box["height"]
    pg.close()


def test_menu_text_uses_the_app_font(mini):
    pg = _page(mini)
    pg.locator('[data-testid="stSelectbox"]').locator("input").click()
    pg.wait_for_timeout(400)
    fonts = pg.evaluate("""() => [...document.querySelectorAll('[role="option"], [role="option"] *')].map(e => getComputedStyle(e).fontFamily)""")
    assert fonts and all("Plus Jakarta Sans" in f for f in fonts)
    pg.close()
