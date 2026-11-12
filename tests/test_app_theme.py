"""The modules split out of streamlit_app.py in Milestone 4 (Nov 2026):
src/app_theme.py (design tokens, stylesheet, minifier) and src/ui_format.py
(text formatting). The minified stylesheet the app sends must mean exactly
what the readable one says: a real browser parses both to the same rules."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app_theme
import ui_format

REPO = Path(__file__).resolve().parent.parent


# ── the stylesheet ──────────────────────────────────────────────────────────

def test_the_app_sends_the_minified_stylesheet():
    app = (REPO / "streamlit_app.py").read_text("utf-8")
    assert "from app_theme import CSS as _CSS, DARK, LIGHT" in app
    assert 'st.html(f"<style>{_CSS}\\n{_root_vars}</style>")' in app
    assert '\n_CSS = """' not in app and "\nLIGHT = {" not in app        # defined once, in app_theme
    assert app_theme.CSS == app_theme.minify_css(app_theme._CSS)
    assert len(app_theme.CSS) < 0.9 * len(app_theme._CSS)


def test_minify_keeps_strings_and_meaningful_spaces():
    css = ('/* a comment */\n@import url(\'https://x.example/a?b=1;c=2,d\');\n'
           '.a  >  .b ,\n .c :hover { content: "x ; { } , y"; margin: 0 auto ; }\n'
           '@media (min-width: 768px) and (max-width: 900px) {\n  .d { border: 1px solid red; }\n}\n')
    out = app_theme.minify_css(css)
    assert "comment" not in out
    assert "url('https://x.example/a?b=1;c=2,d')" in out                     # byte for byte
    assert 'content: "x ; { } , y"' in out
    assert ".a > .b , .c :hover{" in out                                      # descendant ":hover" kept apart
    assert "margin: 0 auto}" in out and "1px solid red" in out
    assert "(min-width: 768px) and (max-width: 900px){" in out
    assert app_theme.minify_css(out) == out                                   # stable


def test_tokens_cover_both_palettes():
    assert set(app_theme.LIGHT) == set(app_theme.DARK) and len(app_theme.LIGHT) > 50
    for name in ("--accent", "--surface", "--text", "--border"):
        assert f"var({name})" in app_theme._CSS


def test_the_browser_parses_both_stylesheets_to_the_same_rules():
    pw = pytest.importorskip("playwright.sync_api")
    rules = """(css) => { const s = document.createElement('style'); s.textContent = css;
      document.head.appendChild(s); const out = [];
      const walk = (list) => { for (const r of list) { out.push(r.cssText); if (r.cssRules) walk(r.cssRules); } };
      walk(s.sheet.cssRules); s.remove(); return out; }"""
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content("<html><head></head><body></body></html>")
            readable = page.evaluate(rules, app_theme._CSS)
            minified = page.evaluate(rules, app_theme.CSS)
        finally:
            browser.close()
    assert len(readable) > 500
    # a declaration that uses var() keeps its source text until it is
    # computed, so the readable sheet's line breaks show up in cssText; a run
    # of whitespace is one token in CSS either way (quoted strings, where it
    # would matter, are kept byte for byte: the test above)
    norm = [" ".join(r.split()) for r in readable]
    assert len(minified) == len(readable)
    assert [" ".join(r.split()) for r in minified] == norm


# ── text formatting ─────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("delta, text", [
    (timedelta(seconds=5), "just now"), (timedelta(minutes=5), "5 min ago"), (timedelta(hours=3), "3h ago"),
    (timedelta(days=1, hours=2), "1 day ago"), (timedelta(days=9), "9 days ago")])
def test_ago(delta, text):
    assert ui_format._ago(NOW - delta, NOW) == text
    assert ui_format._ago(None, NOW) == "never"


def test_india_time_stamps():
    assert ui_format._ist(NOW) == "11:30 AM IST"
    assert ui_format._ist_stamp(NOW) == "Nov 12, 11:30 AM IST"
    assert ui_format._ist_full(NOW) == "12 Nov 2026, 11:30 AM IST"
    assert ui_format.interval_label(1) == "Every hour" and ui_format.interval_label(3) == "Every 3 hours"


def test_found_at_assumes_the_latest_past_date():
    assert ui_format._found_at("Nov 11, 13:45", NOW) == datetime(2026, 11, 11, 13, 45, tzinfo=timezone.utc)
    assert ui_format._found_at("Dec 30, 10:00", NOW).year == 2025
    assert ui_format._found_at("garbage", NOW) is None and ui_format._found_at(None, NOW) is None


def test_urls_plurals_and_icons():
    assert ui_format._host("https://www.example.com/a") == "example.com"
    assert ui_format._short_url("https://www.example.com/" + "x" * 80, 20).endswith("…")
    assert len(ui_format._short_url("https://www.example.com/" + "x" * 80, 20)) == 20
    assert ui_format._plural(1, "job") == "1 job" and ui_format._plural(2, "job") == "2 jobs"
    assert ui_format._plural(2, "company", "companies") == "2 companies"
    assert ui_format._ms("mail", "s16") == '<span class="ms s16" aria-hidden="true">mail</span>'
    assert ui_format._parse_iso("2026-11-12T06:00:00") == NOW and ui_format._parse_iso("x") is None
