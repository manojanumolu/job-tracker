"""Full-screen loader and premium search fields (Oct 2026 visual polish).

The loading layer is the app's own .stApp::after (Streamlit's decoration bar
and toolbar stay hidden); Streamlit's frontend marks the app element
data-test-script-state="running" for every rerun. After a 450 ms pause it
covers the WHOLE viewport — edge to edge — with a soft veil, a purple→blue
frame on all four edges with one highlight travelling round the screen, and
a large glossy briefcase in the exact centre. It takes clicks only while it
is shown, and lets them through the moment the rerun ends. Search fields
share one premium style; other inputs, dropdowns and buttons keep theirs."""
import ast
import base64
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


def _palette(name: str) -> dict:
    node = next(n for n in ast.parse(SRC).body
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == name)
    return ast.literal_eval(node.value)


# ---------------------------------------------------------------------------
# the full-screen loader
# ---------------------------------------------------------------------------

def test_the_loader_covers_the_whole_viewport():
    layer = _rule(".stApp::after")
    assert "position: fixed; inset: 0; box-sizing: border-box" in layer                  # edge to edge
    assert "52px" not in layer and "left: calc(50%" not in layer and "height: 3px" not in layer  # no badge, no line
    assert "border: 5px solid transparent" in layer and "z-index: 1000010" in layer       # above the sidebar too
    assert "@media (max-width: 767px) { .stApp::after { border-width: 3px; } }" in CSS


def test_the_frame_has_four_edges_and_one_travelling_highlight():
    layer = _rule(".stApp::after")
    for angle in ("90deg", "180deg", "270deg", "0deg"):                                   # top, right, bottom, left
        assert f"linear-gradient({angle}, transparent, var(--load-hi), var(--load-blue), transparent)" in layer, angle
    assert ("linear-gradient(135deg, color-mix(in srgb, var(--accent) 55%, transparent), "
            "color-mix(in srgb, var(--load-blue) 55%, transparent)) 0 0 / 100% 100% no-repeat border-box") in layer
    turn = CSS[CSS.index("@keyframes jt-load-turn {"):CSS.index("} }", CSS.index("@keyframes jt-load-turn {")) + 3]
    assert set(re.findall(r"([a-z-]+)\s*:", turn)) == {"background-position"}
    stops = re.findall(r"(\d+)% \{ background-position: ([^;]+); \}", turn)
    assert [s for s, _ in stops] == ["0", "25", "50", "75", "100"]
    # each quarter moves exactly one edge's highlight: top, then right, bottom, left (clockwise)
    lists = [[p.strip() for p in v.split(",")] for _, v in stops]
    moved = [[i for i in range(8) if a[i] != b[i]] for a, b in zip(lists, lists[1:])]
    assert moved == [[3], [4], [5], [6]]
    assert all(lst[0] == "center" and lst[1] == "center" for lst in lists)              # briefcase and glow stay put


def test_a_large_glossy_briefcase_sits_in_the_exact_centre():
    layer = _rule(".stApp::after")
    assert "var(--load-icon) center / clamp(150px, 27vmin, 236px) auto no-repeat padding-box" in layer
    assert "radial-gradient(closest-side, var(--load-glow), var(--load-glow-2) 55%, transparent 100%) center" in layer
    assert layer.index("var(--load-icon)") < layer.index("radial-gradient") < layer.index("var(--load-veil)")
    fn_src = SRC[SRC.index("def _briefcase_uri"):SRC.index("\n\n\nLIGHT = {")]
    ns = {"base64": base64}
    exec(fn_src, ns)                                                                     # the helper alone
    uri = ns["_briefcase_uri"]("#8d80ff", "#5b4cf5", "#3b82f6", "#2a1f8a")
    assert uri.startswith("url('data:image/svg+xml;base64,") and "<" not in uri
    svg = base64.b64decode(uri[len("url('data:image/svg+xml;base64,"):-2]).decode()
    for part in ('id="body"', 'id="gloss"', 'id="clasp"', 'id="sheen"', "feGaussianBlur", "<ellipse"):
        assert part in svg, part                                                         # gradient, gloss, clasp, shadow


def test_the_loader_appears_only_during_real_loading_and_never_sticks():
    hidden = _rule(".stApp::after")
    assert "visibility: hidden; pointer-events: none; opacity: 0" in hidden              # gone when idle
    running = _rule('.stApp[data-test-script-state="running"]::after')
    assert running.startswith('.stApp[data-test-script-state="running"]::after { animation: jt-busy-in 220ms ease-out 450ms both')
    assert "visibility: visible; pointer-events: auto" in running
    assert "transition: visibility 0s linear 450ms" in running                           # clicks pass for the first 450 ms
    assert "transition" not in hidden                                                    # and the moment the rerun ends


def test_reduced_motion_shows_the_same_screen_still():
    reduced = CSS[CSS.index("/* the bar appears (still after the pause)"):]
    reduced = reduced[:reduced.index("}\n}") + 3]
    assert "jt-busy-fade 1ms linear 450ms both" in reduced and "jt-load-turn" not in reduced
    for part in ("var(--load-icon) center / clamp(150px, 27vmin, 236px)", "radial-gradient(closest-side",
                 "var(--load-veil) padding-box", "linear-gradient(135deg, var(--accent), var(--load-blue)) border-box"):
        assert part in reduced, part


def test_both_themes_define_the_loader_and_search_tokens():
    for name in ("LIGHT", "DARK"):
        pal = _palette(name)
        for token in ("load-blue", "load-hi", "load-veil", "load-glow", "load-glow-2", "search-gloss", "search-tint",
                      "search-tint-hover", "search-edge", "search-edge-hover", "search-edge-focus", "search-shadow",
                      "search-shadow-hover", "search-chip", "search-chip-focus", "search-icon", "search-placeholder"):
            assert pal.get(token), (name, token)
        assert re.search(rf'{name}\["load-icon"\] = _briefcase_uri\("#[0-9a-f]{{6}}", "#[0-9a-f]{{6}}", "#[0-9a-f]{{6}}", "#[0-9a-f]{{6}}"\)', SRC)
    assert "<" not in CSS and "@property" not in CSS and "conic-gradient" not in CSS


# ---------------------------------------------------------------------------
# premium search fields
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
