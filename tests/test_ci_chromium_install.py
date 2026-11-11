""".github/scripts/install-chromium.sh — Chromium for CI that survives an apt
mirror outage (Nov 2026).

On 2026-11-11 Ubuntu's Azure mirror stopped resolving from GitHub's runners
and `playwright install chromium --with-deps` failed every scan and test run,
although all of Chromium's libraries were already on the runner image: the
only downloads were optional fonts. The script installs required packages
only if missing (any failure is fatal), the browser (fatal), the optional
fonts best effort (a warning), and then proves Chromium launches (fatal).

The script runs here with fake apt-get / dpkg-query / python / timeout
commands (its APT_GET, DPKG_QUERY, PYTHON and TIMEOUT_CMD hooks), so every
branch — success, an outage, a timeout and each fatal failure — is
exercised without root, network or a browser.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "scripts" / "install-chromium.sh"


def _lists() -> tuple[list[str], list[str]]:
    text = SCRIPT.read_text("utf-8")

    def array(name):
        body = re.search(rf"^{name}=\((.*?)^\)", text, re.S | re.M).group(1)
        return [w for line in body.splitlines() for w in line.split("#", 1)[0].split()]
    return array("REQUIRED_PACKAGES"), array("OPTIONAL_FONT_PACKAGES")


REQUIRED, OPTIONAL = _lists()

FAKE_DPKG = """#!/usr/bin/env bash
# dpkg-query -W -f='${Status}' PKG
pkg="${@: -1}"
if grep -qx "$pkg" "$FAKE_INSTALLED"; then printf 'install ok installed'; else exit 1; fi
"""
FAKE_APT = """#!/usr/bin/env bash
echo "apt $*" >> "$FAKE_LOG"
args=" $* "
case "$args" in
  *" update "*)          [[ "$FAKE_APT_FAIL" == update ]] && exit 100; exit 0 ;;
esac
if [[ "$args" == *" --download-only "* ]]; then
  [[ "$FAKE_APT_FAIL" == download ]] && { echo "E: Temporary failure resolving 'azure.archive.ubuntu.com'" >&2; exit 100; }
  exit 0
fi
if [[ "$args" == *" install "* ]]; then
  [[ "$FAKE_APT_FAIL" == install ]] && exit 100
  [[ "$FAKE_APT_FAIL" == noop_install ]] && exit 0            # "succeeds" but installs nothing
  for w in $*; do case "$w" in -*|install|Acquire::*) ;; *) echo "$w" >> "$FAKE_INSTALLED" ;; esac; done
fi
"""
FAKE_PY = """#!/usr/bin/env bash
echo "python $*" >> "$FAKE_LOG"
if [[ "$1" == "-m" && "$2" == "playwright" ]]; then exit "${FAKE_PW_EXIT:-0}"; fi
exit "${FAKE_VERIFY_EXIT:-0}"
"""
FAKE_TIMEOUT_EXPIRES = """#!/usr/bin/env bash
echo "timeout-expired $*" >> "$FAKE_LOG"
exit 124
"""


@pytest.fixture
def run(tmp_path):
    bins = tmp_path / "bin"
    bins.mkdir()
    for name, body in (("dpkg-query", FAKE_DPKG), ("apt-get", FAKE_APT), ("python", FAKE_PY),
                       ("timeout-expires", FAKE_TIMEOUT_EXPIRES)):
        (bins / name).write_text(body)
        (bins / name).chmod(0o755)
    installed, log = tmp_path / "installed", tmp_path / "log"

    def _run(installed_pkgs, **env):
        installed.write_text("".join(f"{p}\n" for p in installed_pkgs))
        log.write_text("")
        e = {**os.environ, "FAKE_INSTALLED": str(installed), "FAKE_LOG": str(log),
             "APT_GET": str(bins / "apt-get"), "DPKG_QUERY": str(bins / "dpkg-query"),
             "PYTHON": str(bins / "python"), "JT_OPTIONAL_FONT_TIMEOUT_S": "5", **env}
        if env.get("TIMEOUT_CMD") == "expires":
            e["TIMEOUT_CMD"] = str(bins / "timeout-expires")
        p = subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True, timeout=60)
        return p, log.read_text().splitlines(), installed.read_text().split()
    return _run


def _browser_and_verify_ran(calls):
    return (any(c == "python -m playwright install chromium" for c in calls)
            and any(c.startswith("python ") and c.endswith("verify_chromium.py") for c in calls))


# --- the package split ---------------------------------------------------------

def test_split_is_playwrights_own_list_with_only_fonts_optional():
    # Playwright 1.63 for ubuntu24.04 ("chromium" + "tools"), as the runner logs
    # of 2026-11-11 show --with-deps installing
    playwright_chromium = {
        "libasound2t64", "libatk-bridge2.0-0t64", "libatk1.0-0t64", "libatspi2.0-0t64", "libcairo2",
        "libcups2t64", "libdbus-1-3", "libdrm2", "libgbm1", "libglib2.0-0t64", "libnspr4", "libnss3",
        "libpango-1.0-0", "libx11-6", "libxcb1", "libxcomposite1", "libxdamage1", "libxext6", "libxfixes3",
        "libxkbcommon0", "libxrandr2"}
    playwright_tools = {
        "xvfb", "fonts-noto-color-emoji", "fonts-unifont", "libfontconfig1", "libfreetype6", "xfonts-cyrillic",
        "xfonts-scalable", "fonts-liberation", "fonts-ipafont-gothic", "fonts-wqy-zenhei", "fonts-tlwg-loma-otf",
        "fonts-freefont-ttf"}
    assert set(REQUIRED) | set(OPTIONAL) == playwright_chromium | playwright_tools
    assert not set(REQUIRED) & set(OPTIONAL) and len(REQUIRED) == len(set(REQUIRED))
    assert playwright_chromium <= set(REQUIRED)                       # every Chromium library is mandatory
    assert {"libfontconfig1", "libfreetype6", "fonts-liberation"} <= set(REQUIRED)   # the font stack + a base font
    assert all(p.startswith(("fonts-", "xfonts-")) for p in OPTIONAL)                 # only fonts are optional
    # the seven that failed on 2026-11-11 are all optional
    assert {"fonts-freefont-ttf", "fonts-ipafont-gothic", "fonts-tlwg-loma-otf", "fonts-unifont",
            "fonts-wqy-zenhei", "xfonts-cyrillic", "xfonts-scalable"} <= set(OPTIONAL)


def test_split_matches_the_installed_playwright_when_it_can_be_read():
    try:
        import playwright
    except ImportError:
        pytest.skip("playwright not installed")
    bundle = Path(playwright.__file__).parent / "driver" / "package" / "lib" / "coreBundle.js"
    text = bundle.read_text("utf-8") if bundle.is_file() else ""
    i = text.find('"ubuntu24.04-x64"')
    if i < 0:
        pytest.skip("this Playwright build doesn't expose its dependency table here")
    block = text[i:i + 6000]
    theirs = set()
    for key in ("tools", "chromium"):
        theirs |= set(re.findall(r'"([^"]+)"', re.search(key + r"\s*:\s*\[(.*?)\]", block, re.S).group(1)))
    # a Playwright upgrade that changes Chromium's dependencies fails here, so
    # the split is reviewed instead of silently drifting
    assert set(REQUIRED) | set(OPTIONAL) == theirs


# --- successful installation ------------------------------------------------------

def test_everything_present_needs_no_apt_at_all(run):
    p, calls, _ = run(REQUIRED + OPTIONAL)
    assert p.returncode == 0, p.stdout + p.stderr
    assert not any(c.startswith("apt ") for c in calls)          # a mirror outage can't touch this run
    assert _browser_and_verify_ran(calls)
    assert "::warning" not in p.stdout and "::error" not in p.stdout


def test_missing_optional_fonts_are_downloaded_then_installed(run):
    p, calls, installed = run(REQUIRED)
    assert p.returncode == 0, p.stdout + p.stderr
    apt = [c for c in calls if c.startswith("apt ")]
    assert any(" update" in c for c in apt)
    download = [c for c in apt if "--download-only" in c]
    final = [c for c in apt if " install " in c and "--download-only" not in c]
    assert len(download) == 1 and len(final) == 1 and apt.index(download[0]) < apt.index(final[0])
    assert "Acquire::Retries=1" in download[0]                  # fails fast instead of minutes of retries
    # an unreachable mirror must FAIL the update (plain `apt-get update` only
    # warns and exits 0 — which cost 8.7 min on the first outage CI run)
    assert all("--error-on=any" in c for c in apt if " update" in c)
    assert set(OPTIONAL) <= set(installed)
    assert "::warning" not in p.stdout
    assert _browser_and_verify_ran(calls)


def test_missing_required_package_is_installed(run):
    p, calls, installed = run([x for x in REQUIRED if x != "libnss3"] + OPTIONAL)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "libnss3" in installed and _browser_and_verify_ran(calls)
    assert any(c.startswith("apt ") and " install " in c and "libnss3" in c and "--download-only" not in c
               for c in calls)


# --- simulated optional-package failure: warn and carry on ----------------------

@pytest.mark.parametrize("failure", ["download", "update", "install"])
def test_optional_font_failure_is_a_warning_and_the_job_continues(run, failure):
    p, calls, installed = run(REQUIRED, FAKE_APT_FAIL=failure)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "::warning title=Optional fonts not installed::" in p.stdout
    assert "fonts-wqy-zenhei" in p.stdout                       # says exactly what is missing
    assert _browser_and_verify_ran(calls)                        # the browser is still installed and checked
    if failure in ("download", "update"):
        assert not set(OPTIONAL) & set(installed)
    if failure == "update":                                       # a failed update skips the download outright
        assert not any("--download-only" in c for c in calls)


def test_a_hung_mirror_is_cut_off_by_the_timeout(run):
    p, calls, _ = run(REQUIRED, TIMEOUT_CMD="expires")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "apt exited 124" in p.stdout and "::warning" in p.stdout
    assert any(c.startswith("timeout-expired 5 ") for c in calls)
    assert _browser_and_verify_ran(calls)


# --- everything else stays fatal ------------------------------------------------

def test_required_package_install_failure_stops_the_job(run):
    p, calls, _ = run([x for x in REQUIRED if x != "libgbm1"] + OPTIONAL, FAKE_APT_FAIL="install")
    assert p.returncode != 0
    assert not _browser_and_verify_ran(calls)


def test_required_package_still_missing_after_apt_stops_the_job(run):
    p, calls, _ = run([x for x in REQUIRED if x != "libgbm1"] + OPTIONAL, FAKE_APT_FAIL="noop_install")
    assert p.returncode == 1
    assert "::error title=Chromium dependencies missing::still not installed: libgbm1" in p.stdout
    assert not _browser_and_verify_ran(calls)


def test_required_update_failure_stops_the_job_when_something_is_missing(run):
    p, _, _ = run([x for x in REQUIRED if x != "libnss3"] + OPTIONAL, FAKE_APT_FAIL="update")
    assert p.returncode != 0


def test_browser_download_failure_stops_the_job(run):
    p, calls, _ = run(REQUIRED + OPTIONAL, FAKE_PW_EXIT="1")
    assert p.returncode != 0
    assert not any(c.endswith("verify_chromium.py") for c in calls)


def test_launch_check_failure_stops_the_job_even_after_a_font_warning(run):
    p, _, _ = run(REQUIRED, FAKE_APT_FAIL="download", FAKE_VERIFY_EXIT="1")
    assert p.returncode != 0 and "::warning" in p.stdout


# --- the workflows use it, and nothing else changed about them ----------------------

def _job(text: str, name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][\w-]*:\n|\Z)", text, re.S | re.M)
    return m.group(1)


def test_both_workflows_install_chromium_through_the_script():
    tests = (REPO / ".github" / "workflows" / "tests.yml").read_text("utf-8")
    jobs = (REPO / ".github" / "workflows" / "check_jobs.yml").read_text("utf-8")
    for text in (tests, jobs):
        assert "--with-deps" not in text
        assert "playwright install" not in text
    assert "run: bash .github/scripts/install-chromium.sh" in tests
    scrape, publish = _job(jobs, "scrape"), _job(jobs, "publish")
    assert "run: bash .github/scripts/install-chromium.sh" in scrape
    # before the scraper runs, in the job without secrets — never in the publish job
    assert scrape.index("install-chromium.sh") < scrape.index("run: python src/scraper.py")
    assert "install-chromium.sh" not in publish       # (secret-free scrape job: test_jt01_supply_chain)


def test_the_scripts_hold_no_secret_and_never_push():
    for name in ("install-chromium.sh", "verify_chromium.py"):
        text = (REPO / ".github" / "scripts" / name).read_text("utf-8")
        for forbidden in ("secrets", "GITHUB_TOKEN", "git push", "curl ", "wget ", "pip install"):
            assert forbidden not in text, (name, forbidden)


FAKE_TIMEOUT_LOGS = """#!/usr/bin/env bash
echo "timeout $1" >> "$FAKE_LOG"
shift
exec "$@"
"""


def test_the_download_only_gets_what_is_left_of_one_shared_budget(run, tmp_path):
    t = tmp_path / "bin" / "timeout-logs"
    t.write_text(FAKE_TIMEOUT_LOGS)
    t.chmod(0o755)
    p, calls, _ = run(REQUIRED, TIMEOUT_CMD=str(t), JT_OPTIONAL_FONT_TIMEOUT_S="40")
    assert p.returncode == 0, p.stdout + p.stderr
    budgets = [int(c.split()[1]) for c in calls if c.startswith("timeout ")]
    assert len(budgets) == 2 and budgets[0] == 40 and 0 < budgets[1] <= 40      # update, then the remainder
