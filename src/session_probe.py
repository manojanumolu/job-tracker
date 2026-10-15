"""The browser-side reader of the email sign-in's session cookie.

Streamlit Community Cloud keeps cookies an app sets from reaching the
server, so the server can't read jt_session itself. This static component
(assets/session_probe/index.html, same origin as the app) reads only that
cookie in the browser and posts it only to the app's own origin; the
server then checks it like any untrusted input. Declared here, in a real
module, because Streamlit names a component after its declaring module."""
from pathlib import Path

import streamlit.components.v1 as components

PATH = Path(__file__).resolve().parent.parent / "assets" / "session_probe"


def probe(**kwargs):
    """Render the (invisible) probe: its value is the cookie ("" = none),
    or ``default`` until the browser has reported."""
    return components.declare_component("jt_session_probe", path=str(PATH))(**kwargs)
