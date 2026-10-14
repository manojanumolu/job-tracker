"""Full-screen loader (five small centre icons) and premium search fields.

The loading layer is the app's own .stApp::after (Streamlit's decoration bar
and toolbar stay hidden); Streamlit's frontend marks the app element
data-test-script-state="running" for every rerun. After a 450 ms pause it
covers the WHOLE viewport — edge to edge — with a soft solid veil, a thin
purple→blue line on all four edges with one soft highlight travelling round
the screen, and a medium translucent glass briefcase in the exact centre. It takes clicks only while it
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
# the full-screen loader: the 7f407c8 frame + five small centre icons
# ---------------------------------------------------------------------------

ICON_NAMES = ["search", "briefcase", "graduate", "building", "check"]


def _frame_block(src: str) -> str:
    i = src.index('.stApp::after { content: ""; position: fixed; inset: 0;')
    return src[i:src.index("border-box; }", i) + len("border-box; }")]


def test_the_frame_covers_the_whole_viewport():
    layer = _rule(".stApp::after")
    assert "position: fixed; inset: 0; box-sizing: border-box" in layer                  # edge to edge
    assert "52px" not in layer and "left: calc(50%" not in layer and "height: 3px" not in layer  # no badge, no line
    assert "border: 5px solid transparent" in layer and "z-index: 1000010" in layer
    assert "@media (max-width: 767px) { .stApp::after { border-width: 3px; } }" in CSS


def test_the_frame_is_the_one_from_7f407c8():
    """The user asked for the earlier full-screen border back, exactly: its
    thickness, colours, inner shading and travelling highlight (only the
    centre artwork layers of that version are gone)."""
    layer = _frame_block(SRC)
    assert "box-shadow: inset 0 0 34px -10px color-mix(in srgb, var(--accent) 55%, transparent);" in layer
    for angle in ("90deg", "180deg", "270deg", "0deg"):                                   # top, right, bottom, left
        assert f"linear-gradient({angle}, transparent, var(--load-hi), var(--load-blue), transparent)" in layer, angle
    assert ("linear-gradient(135deg, color-mix(in srgb, var(--accent) 55%, transparent), "
            "color-mix(in srgb, var(--load-blue) 55%, transparent)) 0 0 / 100% 100% no-repeat border-box") in layer
    assert _palette("LIGHT")["load-hi"] == "#a89dff" and _palette("DARK")["load-hi"] == "#d4ceff"
    assert "var(--load-icon)" not in SRC and "_briefcase_uri" not in SRC and "load-glow" not in SRC   # the briefcase is gone


def test_one_highlight_travels_round_all_four_edges():
    turn = CSS[CSS.index("@keyframes jt-load-turn {"):CSS.index("} }", CSS.index("@keyframes jt-load-turn {")) + 3]
    assert set(re.findall(r"([a-z-]+)\s*:", turn)) == {"background-position"}
    stops = re.findall(r"(\d+)% \{ background-position: ([^;]+); \}", turn)
    assert [s for s, _ in stops] == ["0", "25", "50", "75", "100"]
    lists = [[p.strip() for p in v.split(",")] for _, v in stops]
    moved = [[i for i in range(6) if a[i] != b[i]] for a, b in zip(lists, lists[1:])]
    assert moved == [[1], [2], [3], [4]]                                                  # clockwise, one edge at a time


def test_five_glossy_icons_in_the_exact_centre():
    """1.5x the earlier 32px line icons (48px in a 60px box), glossy solids:
    a gradient body, a white sheen, a light rim and a darker base."""
    box = _rule(".stApp::before")
    assert "top: calc(50% - 30px); left: calc(50% - 30px); width: 60px; height: 60px" in box
    assert "z-index: 1000011" in box and "pointer-events: none" in box and "visibility: hidden" in box
    layers = re.findall(r"var\(--load-([a-z]+)\) -64px 50% / 48px 48px no-repeat", box)
    assert layers == ICON_NAMES                                                           # five, same size, in order
    assert "drop-shadow(0 3px 8px" in box                                                 # a soft shadow, no halo
    for name in ("LIGHT", "DARK"):
        assert re.search(rf'for _pal, _colours in \(\(LIGHT, .*?\), \(DARK, .*?\)\):', SRC)
    fn_src = SRC[SRC.index("def _loader_icons"):SRC.index("\n\n\nLIGHT = {")]
    ns = {"base64": base64}
    exec(fn_src, ns)
    icons = ns["_loader_icons"]("#6a5cf7", "#3b82f6", "#0d9488")
    assert list(icons) == ICON_NAMES
    for name, uri in icons.items():
        assert uri.startswith("url('data:image/svg+xml;base64,") and "<" not in uri
        svg = base64.b64decode(uri[len("url('data:image/svg+xml;base64,"):-2]).decode()
        assert 'viewBox="0 0 48 48"' in svg, name                                         # one grid
        for layer in ('fill="url(#b)"', 'fill="url(#d)"', 'fill="url(#s)"', 'stroke="#fff" stroke-opacity=".38"'):
            assert layer in svg, (name, layer)                                            # one glossy style
        assert "filter" not in svg and "<image" not in svg and "<text" not in svg, name   # light: shapes only


def test_the_icons_appear_one_at_a_time_in_order_and_loop():
    kf = CSS[CSS.index("@keyframes jt-load-icons {"):CSS.index("} }", CSS.index("@keyframes jt-load-icons {")) + 3]
    assert set(re.findall(r"([a-z-]+)\s*:", kf)) == {"opacity", "transform", "background-position"}
    stops = re.findall(r"(\d+)% \{ opacity: ([.\d]+); transform: scale\(([.\d]+)\); background-position: ([^;]+); \}", kf)
    assert stops and stops[-1][0] == "100"
    for pct, opacity, scale, positions in stops:
        shown = [i for i, pos in enumerate(positions.split(",")) if pos.strip() == "50% 50%"]
        assert len(shown) == 1, pct                                                       # never two at once
        if float(opacity) == 1:
            assert float(scale) == 1                                                      # fully shown = full size
    visible = [ICON_NAMES[[i for i, pos in enumerate(v.split(",")) if pos.strip() == "50% 50%"][0]]
               for _, o, _, v in stops if float(o) == 1]
    order = [n for i, n in enumerate(visible) if i == 0 or visible[i - 1] != n]
    assert order == ICON_NAMES                                                            # search → … → check
    assert all(float(sc) >= .86 for _, _, sc, _ in stops)                                 # a slight scale, no bounce
    running = _rule('.stApp[data-test-script-state="running"]::before')
    assert "jt-load-icons 6s ease-in-out 450ms infinite both" in running                  # loops while loading
    assert "visibility: visible; transition: visibility 0s linear 450ms" in running


def test_the_loader_appears_only_during_real_loading_and_never_sticks():
    hidden = _rule(".stApp::after")
    assert "visibility: hidden; pointer-events: none; opacity: 0" in hidden              # gone when idle
    running = _rule('.stApp[data-test-script-state="running"]::after')
    assert running.startswith('.stApp[data-test-script-state="running"]::after { animation: jt-busy-in 220ms ease-out 450ms both')
    assert "visibility: visible; pointer-events: auto" in running
    assert "transition: visibility 0s linear 450ms" in running                           # clicks pass for the first 450 ms
    assert "transition" not in hidden and "transition" not in _rule(".stApp::before")    # and the moment the rerun ends


def test_reduced_motion_shows_a_still_frame_and_one_still_icon():
    reduced = CSS[CSS.index("/* the bar appears (still after the pause)"):]
    reduced = reduced[:reduced.index("}\n}") + 3]
    assert "jt-load-turn" not in reduced and "jt-load-icons" not in reduced
    assert reduced.count("jt-busy-fade 1ms linear 450ms both") == 2
    assert "var(--load-veil) padding-box, linear-gradient(135deg, var(--accent), var(--load-blue)) border-box" in reduced
    still = re.search(r'\.stApp\[data-test-script-state="running"\]::before \{[^}]*background: ([^!]+) !important', reduced)
    shown = [l for l in still.group(1).split(", var") if "50% 50%" in l]
    assert len(shown) == 1 and "briefcase" in shown[0]


def test_both_themes_define_the_loader_tokens():
    for name in ("LIGHT", "DARK"):
        pal = _palette(name)
        for token in ("load-blue", "load-hi", "load-veil", "search-gloss", "search-tint", "search-tint-hover", "search-edge",
                      "search-edge-hover", "search-edge-focus", "search-shadow", "search-shadow-hover", "search-chip",
                      "search-chip-focus", "search-icon", "search-placeholder"):
            assert pal.get(token), (name, token)
        assert "rgba" not in pal["load-veil"]                                             # a clean, solid backdrop
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
