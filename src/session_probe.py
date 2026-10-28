"""The browser-side reader of the email sign-in's session cookie.

Streamlit Community Cloud keeps cookies an app sets from reaching the
server, so the server can't read jt_session itself. This static component
(assets/session_probe/index.html, same origin as the app) reads only that
cookie in the browser and posts it only to the app's own origin; the
server then checks it like any untrusted input. Declared here, in a real
module, because Streamlit names a component after its declaring module.

Declared ONCE, when this module is imported (Streamlit's documented
pattern), not on every call: declare_component looks its caller's module
up in sys.modules (inspect.getmodule) and raises "module is None. This
should never happen." when the module isn't there. That happened in
production (28 Oct 2026) — the app drops its own modules from sys.modules
to pick up a deploy (streamlit_app._drop_moved_local_modules), and
Streamlit's file watcher does the same for a changed file — so a call that
ran after the removal took the whole sign-in gate down. During its own
import a module is always in sys.modules; after that, probe() never looks
it up again. The app imports this module only inside a script run, so the
declaration also registers the component with the running app; a re-import
after a removal declares (and registers) it again under the same name."""
from pathlib import Path

import streamlit.components.v1 as components

PATH = Path(__file__).resolve().parent.parent / "assets" / "session_probe"

COMPONENT = components.declare_component("jt_session_probe", path=str(PATH))


def probe(**kwargs):
    """Render the (invisible) probe: its value is the cookie ("" = none),
    or ``default`` until the browser has reported."""
    return COMPONENT(**kwargs)
