"""The loader (a thin bar along the top) and premium search fields.

The loading bar is the app's own .stApp::after (Streamlit's decoration bar
and toolbar stay hidden); Streamlit's frontend marks the app element
data-test-script-state="running" for every rerun. After a 450 ms pause a
3px purple→teal bar slides along the top edge; it never takes clicks.
Search fields share one premium style; other inputs, dropdowns and buttons
keep theirs."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "streamlit_app.py").read_text("utf-8")
CSS = SRC[SRC.index('_CSS = """') + len('_CSS = """'):SRC.index('"""', SRC.index('_CSS = """') + 10)]
SEARCH = '.stTextInput:has(input[aria-label^="Search"])'
FIELD = f'{SEARCH} :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])'


def _rule(selector: str, css: str = CSS) -> str:
    start = css.index(selector + " {")
    return css[start:css.index("}", start)]


# ---------------------------------------------------------------------------
# the loader: the thin top bar of e4c241a, restored 2026-10-15
# ---------------------------------------------------------------------------
# The user found the full-screen loader (veil, four-edge frame, centre
# icons) distracting: it covered the page on every sign-in and fresh page
# load. The earlier behaviour is back: a 3px bar sliding along the top edge
# after a 450 ms pause, never blocking clicks.

def test_the_loader_is_one_thin_bar_along_the_top():
    bar = _rule(".stApp::after")
    for part in ("position: fixed", "top: 0", "left: 0", "right: 0", "height: 3px", "pointer-events: none", "opacity: 0"):
        assert part in bar, part
    assert "inset: 0" not in bar and "border:" not in bar                  # no full-screen layer, no frame
    shown = _rule('.stApp[data-test-script-state="running"]::after')
    assert "jt-busy-in 220ms ease-out 450ms both" in shown and "jt-busy-slide 1.15s linear 450ms infinite" in shown
    assert "@keyframes jt-busy-slide { from { background-position: -45% 0; } to { background-position: 145% 0; } }" in CSS


def test_no_full_screen_loader_remains():
    for gone in ("load-veil", "load-search", "load-briefcase", "jt-load-turn", "jt-load-icons", ":has(.jt-skel)::after",
                 ':has(.jt-skel)::before', '[data-testid="stMain"]::before'):
        assert gone not in CSS, gone
    assert "_loader_icons" not in SRC
    assert not re.search(r"\.stApp::before \{", CSS)


def test_reduced_motion_keeps_a_still_line():
    reduced = CSS[CSS.index("/* the bar appears (still after the pause)"):]
    reduced = reduced[:reduced.index("}\n}") + 3]
    assert "jt-busy-slide" not in reduced and "animation: jt-busy-in 1ms linear 450ms both !important" in reduced


# ---------------------------------------------------------------------------

def test_search_fields_have_the_glossy_surface_and_gradient_edge():
    normal = _rule(FIELD)
    assert ("background: var(--search-gloss) padding-box, var(--search-tint) padding-box, var(--search-edge) border-box !important"
            in normal)
    assert "border: 1.5px solid transparent !important" in normal and "border-radius: 16px !important" in normal
    assert "min-height: 52px !important" in normal and "box-shadow: var(--search-shadow) !important" in normal
    hover = _rule(FIELD + ":hover")
    assert "var(--search-tint-hover)" in hover and "var(--search-edge-hover)" in hover
    focus = _rule(FIELD + ":focus-within")
    assert "var(--search-edge-focus)" in focus and "0 0 0 4px color-mix(in srgb, var(--accent) 16%, transparent)" in focus


def test_the_icon_sits_in_its_own_chip():
    chip = _rule(FIELD + "::before")
    assert 'content: "search"' in chip and "width: 34px !important; height: 34px; line-height: 34px !important" in chip
    assert "align-self: center" in chip and "background: var(--search-chip)" in chip and "color: var(--search-icon) !important" in chip
    assert "background: var(--search-chip-focus)" in _rule(FIELD + ":focus-within::before")


def test_input_and_placeholder_use_the_app_font_explicitly():
    text = _rule(f"{SEARCH} input")
    placeholder = _rule(f"{SEARCH} input::placeholder")
    for rule in (text, placeholder):
        assert "font-family: var(--font) !important" in rule and "font-size: 15px !important" in rule
        assert "font-weight: 500 !important" in rule
    assert "color: var(--search-placeholder) !important" in placeholder and "opacity: 1" in placeholder
    assert "--font: 'Plus Jakarta Sans'" in SRC


def test_only_search_fields_are_restyled():
    start = CSS.index("/* search fields (every")
    block = CSS[start:CSS.index("cursor: not-allowed; }", start) + 22]
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    selectors = [s.split("{")[0] for s in block.split("}") if "{" in s]
    assert selectors and all('input[aria-label^="Search"]' in s for s in selectors)
    assert not any(word in s.lower() for s in selectors for word in ("select", "button"))
    assert sorted(set(re.findall(r'st\.text_input\("(Search[^"]*)"', SRC))) == ["Search", "Search companies"]
