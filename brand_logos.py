"""Company marks for well-known employers, drawn from local SVG.

Each mark is a small SVG kept in this file and served to the browser as a
``data:`` URI inside an <img> (inline <svg> elements don't paint in this
app's hosting), so no image is ever fetched at runtime. A company without a
mark gets initials on a tint instead (see streamlit_app._avatar).

Only names that really are the company match: the comparison is on whole
words from the start of the name ("MetLife" is not Meta, "Intellect" is
not Intel), and the company's name itself is never changed.

No Streamlit import here, so this is unit-testable on its own.
"""

from __future__ import annotations

import base64
import functools
import re

# Google's "G" (also the Google sign-in button's mark)
GOOGLE_G_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">'
    '<path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 '
    '2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>'
    '<path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 '
    '7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>'
    '<path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 '
    '0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>'
    '<path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 '
    '0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>')

_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">{}</svg>'
_SANS = "font-family=\"Arial, Helvetica, 'Liberation Sans', sans-serif\""


def _ibm() -> str:
    # the striped wordmark: blue letters cut by white bars
    top, height, bars = 17.0, 14.4, 8
    step = height / bars
    cuts = "".join(f'<rect x="0" y="{top + step * k - 0.3:.2f}" width="48" height="0.6" fill="#fff"/>'
                   for k in range(1, bars))
    return _SVG.format(f'<text x="24" y="{top + height:.1f}" text-anchor="middle" font-size="20" font-weight="900" '
                       f'letter-spacing="-.4" fill="#0F62FE" {_SANS}>IBM</text>{cuts}')


# key -> (svg, tile background). Tiles are light unless the brand's own
# mark sits on a dark or coloured ground; either way they look the same in
# light and dark mode.
_MARKS: dict[str, tuple[str, str]] = {
    "google": (GOOGLE_G_SVG, "#ffffff"),
    "microsoft": (_SVG.format(
        '<rect x="3" y="3" width="20" height="20" fill="#F25022"/><rect x="25" y="3" width="20" height="20" fill="#7FBA00"/>'
        '<rect x="3" y="25" width="20" height="20" fill="#00A4EF"/><rect x="25" y="25" width="20" height="20" fill="#FFB900"/>'),
        "#ffffff"),
    "amazon": (_SVG.format(
        f'<text x="24" y="28" text-anchor="middle" font-size="30" font-weight="700" fill="#ffffff" {_SANS}>a</text>'
        '<path d="M9.5 32.5c8.6 6 20.4 6.4 28.8 .6" fill="none" stroke="#FF9900" stroke-width="3.2" stroke-linecap="round"/>'
        '<path d="M33.4 30.4l5.4 2.3-1.4 5.6" fill="none" stroke="#FF9900" stroke-width="3" stroke-linecap="round" '
        'stroke-linejoin="round"/>'),
        "#232F3E"),
    "apple": ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="-1 -1 26 26"><path fill="#111111" d="M12.152 6.896c-.948 '
              '0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 '
              '2.208 3.09 3.792 3.039 1.52-.065 2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 '
              '3.676-2.948 1.156-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 '
              '2.597-4.559-1.429-2.09-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 '
              '1.4-2.427 1.245-3.83-1.207.052-2.662.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 '
              '2.715-.688 3.559-1.701"/></svg>', "#ffffff"),
    "meta": (_SVG.format(
        '<defs><linearGradient id="m" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#0064E0"/>'
        '<stop offset="1" stop-color="#0082FB"/></linearGradient></defs>'
        '<path d="M6.5 29.5c0-7.4 3.9-14 9.2-14 4.6 0 7.6 4.5 11.2 10.4 3.3 5.5 5.6 8.1 8.6 8.1 3.4 0 6-3 6-7.6 '
        '0-6.4-3.4-10.9-8.2-10.9-4.4 0-7.4 4.6-11 10.5-3.3 5.5-5.7 8-9.2 8-3.9 0-6.6-1.9-6.6-4.5z" fill="none" '
        'stroke="url(#m)" stroke-width="4.4" stroke-linejoin="round"/>'),
        "#ffffff"),
    "nvidia": (_SVG.format(
        '<path d="M4 24c5.6-8.6 13-12.6 20-12.6S38.4 15.4 44 24c-5.6 8.6-13 12.6-20 12.6S9.6 32.6 4 24z" fill="none" '
        'stroke="#ffffff" stroke-width="3"/>'
        '<path d="M24 16.6a7.4 7.4 0 1 0 7.4 7.4" fill="none" stroke="#ffffff" stroke-width="3" stroke-linecap="round"/>'
        '<circle cx="24" cy="24" r="2.6" fill="#ffffff"/>'),
        "#76B900"),
    "ibm": (_ibm(), "#ffffff"),
    "intel": (_SVG.format(
        f'<text x="22.5" y="29.5" text-anchor="middle" font-size="17" font-weight="700" letter-spacing="-.3" '
        f'fill="#0068B5" {_SANS}>intel</text><rect x="39.6" y="26.6" width="2.9" height="2.9" fill="#0068B5"/>'),
        "#ffffff"),
    "accenture": (_SVG.format(
        '<path d="M15 11.5 36 24 15 36.5" fill="none" stroke="#A100FF" stroke-width="6.5" stroke-linejoin="miter"/>'),
        "#ffffff"),
    "deloitte": (_SVG.format(
        f'<text x="20.5" y="34" text-anchor="middle" font-size="30" font-weight="700" fill="#000000" {_SANS}>D</text>'
        '<circle cx="35" cy="31" r="3.8" fill="#86BC25"/>'),
        "#ffffff"),
}

# key -> the brand's name, for alt text and tooltips
BRAND_NAMES = {"google": "Google", "microsoft": "Microsoft", "amazon": "Amazon", "apple": "Apple", "meta": "Meta",
               "nvidia": "NVIDIA", "ibm": "IBM", "intel": "Intel", "accenture": "Accenture", "deloitte": "Deloitte"}

# names (as whole words at the start of a company's name) -> mark
_ALIASES: dict[str, tuple[str, ...]] = {
    "google": ("google", "alphabet"),
    "microsoft": ("microsoft",),
    "amazon": ("amazon", "aws", "amazon web services"),
    "apple": ("apple",),
    "meta": ("meta", "meta platforms", "facebook"),
    "nvidia": ("nvidia",),
    "ibm": ("ibm", "international business machines"),
    "intel": ("intel",),
    "accenture": ("accenture",),
    "deloitte": ("deloitte",),
}
_ALIAS_WORDS = sorted(((tuple(a.split()), key) for key, names in _ALIASES.items() for a in names),
                      key=lambda pair: -len(pair[0]))


def _words(name: object) -> tuple[str, ...]:
    text = name if isinstance(name, str) else ""
    return tuple(re.findall(r"[a-z0-9]+", text.lower().replace("&", " and ")))


def brand_key(name: object) -> str | None:
    """The mark for a company name, or None. "Google India", "Amazon Web
    Services" and "Deloitte USI" match; "MetLife", "Intellect Design" and
    "Pineapple" don't."""
    words = _words(name)
    for alias, key in _ALIAS_WORDS:
        if words[:len(alias)] == alias:
            return key
    return None


def brand_svg(key: str) -> str:
    return _MARKS[key][0]


def brand_tile(key: str) -> str:
    return _MARKS[key][1]


@functools.lru_cache(maxsize=None)
def brand_uri(key: str) -> str:
    """The mark as a data: URI (cached: each is encoded once per process)."""
    return "data:image/svg+xml;base64," + base64.b64encode(_MARKS[key][0].encode("utf-8")).decode("ascii")


def initials(name: object) -> str:
    """Initials for a company without a mark: the first letters of its first
    two words ("Morgan Stanley" -> "MS"), or the first two letters of a
    one-word name ("MetLife" -> "Me", "Myntra" -> "My"), so companies that
    share a first letter still look different. "" when nothing is usable."""
    words = [w for w in re.findall(r"[^\W_]+", name if isinstance(name, str) else "")
             if w.lower() not in _STOPWORDS] or re.findall(r"[^\W_]+", name if isinstance(name, str) else "")
    if not words:
        return ""
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    w = words[0]
    if w.isupper() and len(w) <= 4:              # PwC-style acronyms read as written: NPCI -> NP
        return w[:2]
    return w[0].upper() + (w[1:2].lower() if len(w) > 1 else "")


_STOPWORDS = {"the", "of", "and", "inc", "ltd", "llc", "llp", "plc", "pvt", "private", "limited", "co", "corp",
              "corporation", "company", "group", "india"}
