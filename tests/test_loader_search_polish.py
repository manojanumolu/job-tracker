"""Loading square and glossy search fields (Oct 2026 visual polish).

The loading indicator is the app's own .stApp::after (Streamlit's decoration
bar and toolbar stay hidden); it shows while Streamlit marks the app element
data-test-script-state="running". It is a rounded square with a frame on all
four sides, a travelling highlight and a centred briefcase — never a line —
and it can't take a click. Search fields share one glossy style; other
inputs, dropdowns and buttons keep theirs."""
import ast
import base64
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "streamlit_app.py").read_text("utf-8")
CSS = SRC[SRC.index('_CSS = """') + len('_CSS = """'):SRC.index('"""', SRC.index('_CSS = """') + 10)]
SEARCH = '.stTextInput:has(input[aria-label^="Search"])'


def _rule(selector: str, css: str = CSS) -> str:
    start = css.index(selector + " {")
    return css[start:css.index("}", start)]


def _palette(name: str) -> dict:
    tree = ast.parse(SRC)
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == name)
    return ast.literal_eval(node.value)


# ---------------------------------------------------------------------------
# the loading square
# ---------------------------------------------------------------------------

def test_the_loader_is_a_rounded_square_not_a_line():
    box = _rule(".stApp::after")
    assert "width: 52px; height: 52px" in box and "border-radius: 15px" in box and "border: 2px solid transparent" in box
    assert "height: 3px" not in box and "right: 0" not in box                            # the old top line is gone
    assert "pointer-events: none" in box and "opacity: 0" in box                         # never clickable, hidden by default
    # four side highlights (one per direction) under the face, and the full frame
    for angle in ("90deg", "180deg", "270deg", "0deg"):
        assert f"linear-gradient({angle}, transparent, var(--load-hi), var(--load-blue), transparent)" in box, angle
    assert "linear-gradient(135deg, color-mix(in srgb, var(--accent) 45%, transparent), color-mix(in srgb, var(--load-blue) 45%, transparent))" in box
    assert box.index("var(--load-icon) center / 24px 24px") < box.index("var(--load-face)") < box.index("linear-gradient(90deg")
    assert "box-shadow: var(--load-glow)" in box


def test_the_loader_only_shows_while_a_rerun_takes_a_moment():
    running = re.search(r'\.stApp\[data-test-script-state="running"\]::after \{ animation: jt-busy-in 220ms ease-out (\d+)ms both, '
                        r'jt-load-turn ([\d.]+)s linear (\d+)ms infinite; \}', CSS)
    assert running and int(running.group(1)) >= 400 and running.group(1) == running.group(3)
    assert 1.2 <= float(running.group(2)) <= 2.5                                         # smooth, never a flash
    turn = CSS[CSS.index("@keyframes jt-load-turn {"):CSS.index("} }", CSS.index("@keyframes jt-load-turn {")) + 3]
    assert set(re.findall(r"([a-z-]+)\s*:", turn)) == {"background-position"}            # moves highlights only
    assert turn.count("center, 0 0,") == 2                                               # briefcase and face never move


def test_phones_get_the_square_below_the_page_navigation():
    assert "@media (max-width: 767px) { .stApp::after { top: auto; bottom: 22px; } }" in CSS


def test_reduced_motion_keeps_a_still_square():
    reduced = CSS[CSS.index("/* the bar appears (still after the pause)"):]
    reduced = reduced[:reduced.index("}\n}") + 3]
    assert "jt-busy-fade 1ms linear 450ms both" in reduced and "jt-load-turn" not in reduced
    assert "var(--load-icon) center / 24px 24px" in reduced and "linear-gradient(135deg, var(--accent), var(--load-blue))" in reduced
    fade = CSS[CSS.index("@keyframes jt-busy-fade"):CSS.index("}", CSS.index("@keyframes jt-busy-fade") + 40) + 2]
    assert "transform" not in fade                                                       # no movement at all


def test_both_themes_have_a_briefcase_and_the_tokens():
    for name in ("LIGHT", "DARK"):
        pal = _palette(name)
        for token in ("load-blue", "load-hi", "load-face", "load-glow", "search-bg", "search-border",
                      "search-border-hover", "search-shadow", "search-shadow-hover", "search-icon"):
            assert pal.get(token), (name, token)
        call = re.search(rf'{name}\["load-icon"\] = _briefcase_uri\("(#[0-9a-f]{{6}})", "(#[0-9a-f]{{6}})"\)', SRC)
        assert call, name
    fn_src = SRC[SRC.index("def _briefcase_uri"):SRC.index("\n\n\nLIGHT = {")]
    ns = {"base64": base64}
    exec(fn_src, ns)                                                                     # the helper alone, not the app
    uri = ns["_briefcase_uri"]("#5b4cf5", "#3b82f6")
    assert uri.startswith("url('data:image/svg+xml;base64,") and uri.endswith("')")
    svg = base64.b64decode(uri[len("url('data:image/svg+xml;base64,"):-2]).decode()
    assert "linearGradient" in svg and 'fill="url(#g)"' in svg and "#5b4cf5" in svg and "#3b82f6" in svg
    assert "<" not in uri                                                                # safe inside the <style> block


def test_the_stylesheet_stays_parseable_and_within_the_motion_rules():
    assert "<" not in CSS and "@property" not in CSS and "conic-gradient" not in CSS


# ---------------------------------------------------------------------------
# glossy search fields
# ---------------------------------------------------------------------------

FIELD = f'{SEARCH} :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])'


def test_search_fields_are_glossy_in_every_state():
    normal = _rule(FIELD)
    assert "background: var(--search-bg) !important" in normal and "border: 1px solid var(--search-border) !important" in normal
    assert "box-shadow: var(--search-shadow) !important" in normal
    assert "border-color: var(--search-border-hover) !important" in _rule(FIELD + ":hover")
    focus = _rule(FIELD + ":focus-within")
    assert "border-color: var(--accent) !important" in focus and "var(--ring)" in focus
    disabled = _rule(f'.stTextInput:has(input[aria-label^="Search"]:disabled) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])')
    assert "box-shadow: none !important" in disabled and "cursor: not-allowed" in disabled


def test_search_text_uses_the_site_font_and_a_visible_icon():
    text = _rule(f"{SEARCH} input")
    assert "font-family: var(--font) !important" in text and "font-weight: 500 !important" in text
    assert "caret-color: var(--accent)" in text
    assert "font-weight: 400" in _rule(f"{SEARCH} input::placeholder")
    assert "color: var(--search-icon) !important" in _rule(FIELD + "::before")
    assert "color: var(--accent-text) !important" in _rule(FIELD + ":focus-within::before")


def test_only_search_fields_are_restyled():
    start = CSS.index("/* search fields (every")
    block = CSS[start:CSS.index("cursor: not-allowed; }", start) + 22]
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)                                  # rules only, not the comment
    selectors = [s.split("{")[0] for s in block.split("}") if "{" in s]
    assert selectors and all('input[aria-label^="Search"]' in s for s in selectors)
    assert not any(word in s.lower() for s in selectors for word in ("select", "button", "baseweb=\"select"))
    # every search box in the app carries a "Search…" label
    labels = re.findall(r'st\.text_input\("(Search[^"]*)"', SRC)
    assert sorted(set(labels)) == ["Search", "Search companies"]
