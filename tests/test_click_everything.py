"""Every page, every button: signed in as an admin and as a member, each
page is opened and one enabled button of each kind is pressed, one after
another as a person would (back on the page before each press), plus one
more run after it. Nothing may raise. The account menu's
Sign out is left out (it ends the session); destructive admin buttons only
reach their confirmation step or act on the test's own temporary copy.
In-memory stores: nothing is emailed and production is never touched."""
import re

import pytest

from test_account_onboarding import JOBS, _ready
from test_final_polish import ustore  # noqa: F401  (fixture)
from test_firebase_auth import _buttons, admins, google_env, no_env  # noqa: F401  (fixtures)
from test_phase3_user_data import A_MAIL, A_UID, B_MAIL, B_UID, _sign_in
from test_streamlit_app import app  # noqa: F401

PAGES = ["home", "jobs", "companies", "monitoring", "email", "settings", "alerts", "preferences"]
SKIP = {"btn_account_sign_out"}


def _open(at, page) -> bool:
    keys = _buttons(at)
    for key in (f"nav_{page}", f"acct_{page}"):
        if key in keys:
            at.button(key=key).click().run()
            return True
    return False


@pytest.mark.parametrize("role", ["admin", "member"])
def test_every_button_on_every_page_works(app, google_env, ustore, admins, role):
    uid, mail = (A_UID, A_MAIL) if role == "admin" else (B_UID, B_MAIL)
    if role == "admin":
        admins(A_MAIL)
    _ready(ustore, uid, mail)
    problems, pressed = [], 0
    for page in PAGES:
        at = _sign_in(app, google_env, uid, mail, seen=JOBS)
        if not _open(at, page):
            continue                                        # not this role's page
        assert not at.exception, (role, page, [e.message for e in at.exception])
        buttons, kinds = [], set()
        for k in sorted(b.key for b in at.button
                        if b.key and not b.disabled and b.key not in SKIP and not b.key.startswith(("nav_", "mob_", "acct_"))):
            # one of each kind: every job card's Details / Dismiss, every avatar tile, ... behave alike
            kind = re.sub(r"_[0-9a-f]{16}$", "", k)
            kind = "av_pick" if kind.startswith("av_pick_") else kind
            if kind not in kinds:
                kinds.add(kind)
                buttons.append(k)
        t = at                                              # one session per page, back on it before each press
        for key in buttons:
            if t.session_state["page"] != page or t.session_state["job_id"] or t.session_state["company_id"]:
                _open(t, page)
            b = next((x for x in t.button if x.key == key), None)
            if b is None or b.disabled:
                continue
            b.click().run()
            pressed += 1
            if t.exception:
                problems.append((page, key, [e.message for e in t.exception]))
                continue
            t.run()                                         # and the run after it
            if t.exception:
                problems.append((page, key + " (next run)", [e.message for e in t.exception]))
    assert pressed > 12 and not problems, problems
