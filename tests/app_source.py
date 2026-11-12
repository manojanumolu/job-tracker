"""The app's source for tests that read it as text (Nov 2026, Milestone 4).

streamlit_app.py imports its design tokens and stylesheet from
src/app_theme.py and its text formatting from src/ui_format.py. Tests that
check the app's source (a CSS rule, a helper's definition) read all three as
one text, in this order, so they keep checking the same code wherever it
lives. Each part is a complete module, so the joined text is valid Python
for the tests that parse it.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_PARTS = ("streamlit_app.py", "src/app_theme.py", "src/ui_format.py")


def app_source() -> str:
    return "\n\n".join((REPO / part).read_text("utf-8") for part in APP_PARTS)
