"""Prove the installed Chromium works the way the scraper uses it (CI only).

Run by install-chromium.sh after the browser and its dependencies are in
place; a non-zero exit fails the job. No network: the page is built in
memory. It checks what src/scraper.py relies on:

  * a headless launch (scraper._scan_playwright: chromium.launch(headless=True))
  * DOM text read back exactly, including non-Latin text — text extraction
    doesn't depend on which fonts are installed, only painting does
  * schema.org JSON-LD read from a <script> block, as scraper._JSONLD_JS does
  * a screenshot, so the rendering and font stack (fontconfig, freetype,
    at least one usable font) is exercised end to end
"""
from __future__ import annotations

import json
import subprocess
import sys

TEXT = "Graduate Engineer Trainee — Hyderabad · 新卒 · Выпускник · ✓"
POSTING = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Graduate Engineer Trainee"}
PAGE = (f'<!doctype html><meta charset="utf-8"><p id="t">{TEXT}</p>'
        f'<script type="application/ld+json">{json.dumps(POSTING)}</script>')


def _font_families() -> int:
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return -1
    return len({line.strip() for line in out.splitlines() if line.strip()})


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(PAGE)
            text = page.inner_text("#t")
            postings = page.evaluate(
                "() => [...document.querySelectorAll('script[type=\"application/ld+json\"]')]"
                ".map(s => JSON.parse(s.textContent))")
            png = page.screenshot()
            version = browser.version
        finally:
            browser.close()
    problems = []
    if text != TEXT:
        problems.append(f"DOM text came back as {text!r}")
    if postings != [POSTING]:
        problems.append(f"JSON-LD came back as {postings!r}")
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) < 1000:
        problems.append("the screenshot isn't a rendered PNG")
    if problems:
        print("::error title=Chromium launch check failed::" + "; ".join(problems))
        return 1
    print(f"Chromium {version} launched headless: DOM text, JSON-LD and rendering OK "
          f"({_font_families()} font families available).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
