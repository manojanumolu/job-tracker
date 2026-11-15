"""M6 responsive layout: every section's HTML, exactly as a signed-in admin
session draws it (AppTest), rendered in Chromium with the app's own
stylesheet at phone and desktop widths. Nothing may be wider than the
viewport (no horizontal page scroll); tables become stacked cards on
phones. Streamlit's own widgets are responsive by themselves; this checks
the M6 markup and CSS added on top of them."""

import pytest

from test_m6_app import JOBS, _ri, _seed
from test_application_tracker import A_MAIL, two_admins  # noqa: F401  (fixture)
from test_firebase_auth import no_env  # noqa: F401
from test_phase3_user_data import _sign_in, admins, app, google_env, store  # noqa: F401
from test_streamlit_app import _html

A_UID = "AdminAaaaaaaaaaaaaaaaaaaaaaa1"
SECTIONS = ("overview", "resumes", "match", "gaps", "interview", "improve", "history")


def _pages(app, google_env, store):
    meta = _seed(store)
    lib = store.for_uid(A_UID).resume_library(lambda: True)
    lib.add_history(meta["id"], "match", "Software Engineer Resume vs Associate Software Engineer — ExampleSoft: 88% (Strong)", {})
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    out = {}
    for s in SECTIONS:
        _ri(at, s)
        out[s] = _html(at)
    at.session_state["ri_review"] = meta["id"]
    at.pills(key="ri_section").set_value("resumes").run()
    out["review"] = _html(at)
    import hashlib

    import config_store
    at.session_state["ri_job"] = hashlib.sha1(config_store.job_key(JOBS[1]).encode()).hexdigest()[:12]
    at.pills(key="ri_section").set_value("match").run()
    out["detail"] = _html(at)
    return out


@pytest.mark.parametrize("width", [360, 390, 768, 1280])
def test_no_m6_section_overflows_the_viewport(app, google_env, store, two_admins, width, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    pages = _pages(app, google_env, store)
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:                                   # pragma: no cover - depends on the machine
            pytest.skip(f"chromium unavailable: {e}")
        page = browser.new_page(viewport={"width": width, "height": 900})
        for name, html in pages.items():
            page.set_content('<!doctype html><html><head><meta name="viewport" content="width=device-width">'
                             '</head><body style="margin:0"><main style="padding:0 16px;max-width:1200px">'
                             f'{html}</main></body></html>')
            over = page.evaluate("""() => {
                const vw = document.documentElement.clientWidth;
                const bad = [];
                for (const el of document.querySelectorAll('main *')) {
                  const r = el.getBoundingClientRect();
                  if (r.width > 0 && r.right > vw + 1 && getComputedStyle(el).position !== 'fixed') {
                    bad.push(el.tagName + '.' + el.className + ' ' + Math.round(r.right));
                  }
                }
                return {scroll: document.documentElement.scrollWidth, vw, bad: bad.slice(0, 5)};
            }""")
            assert over["scroll"] <= over["vw"] + 1 and not over["bad"], (name, width, over)
            if width <= 390:
                display = page.evaluate("(() => { const r = document.querySelector('.ri-table tbody tr'); "
                                        "return r ? getComputedStyle(r).display : null; })()")
                assert display in (None, "block"), (name, display)        # tables become stacked cards on phones
            if name in ("detail", "review") and width in (390, 1280):
                page.screenshot(path=str(tmp_path / f"m6-{name}-{width}.png"), full_page=True)
        browser.close()
