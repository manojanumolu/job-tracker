#!/usr/bin/env bash
# Install Playwright's Chromium for CI, then prove it launches.
#
# Replaces `playwright install chromium --with-deps` in tests.yml and the
# scrape job of check_jobs.yml (Nov 2026). --with-deps installs Playwright's
# whole Ubuntu 24.04 list in one apt transaction, so when Ubuntu's Azure mirror
# stopped resolving (2026-11-11 13:50 UTC onwards) every scan and test run
# failed — although every library Chromium needs was already on the runner
# image. The only downloads were seven optional fonts (CJK, Thai, Cyrillic,
# Unicode fallback) and two helpers they pull in. Those fonts change how
# non-Latin glyphs are PAINTED; the scraper reads DOM text and JSON-LD, which
# no font changes.
#
# So the list is split, verbatim from Playwright 1.63 (the version the lock
# files pin; tests/test_ci_chromium_install.py checks the split against
# Playwright's own list):
#   REQUIRED  Chromium's shared libraries, fontconfig/freetype and one base
#             Latin font family, plus xvfb (kept as before; headless Chromium
#             doesn't use it). Installed only if missing; any failure stops
#             the job.
#   OPTIONAL  the extra font families. Best effort: the network part has one
#             time budget (default 180 s) and a failure is a workflow
#             warning, never a failed job.
# The browser download and the launch check stay mandatory: whatever apt did,
# the job only continues with a Chromium that starts and renders a page.
#
# Test hooks (tests/test_ci_chromium_install.py): APT_GET, DPKG_QUERY,
# PYTHON and TIMEOUT_CMD name the commands to run; JT_OPTIONAL_FONT_TIMEOUT_S
# is the optional fonts' total network budget (default 180 s).
set -euo pipefail

REQUIRED_PACKAGES=(
  # Playwright 1.63 "chromium" list for ubuntu24.04
  libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2
  libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3
  libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3
  libxkbcommon0 libxrandr2
  # from its "tools" list: the font stack Chromium links against, a base
  # Latin font family, and xvfb
  libfontconfig1 libfreetype6 fonts-liberation xvfb
)
OPTIONAL_FONT_PACKAGES=(
  # the rest of its "tools" list: extra scripts and symbols
  fonts-noto-color-emoji fonts-unifont xfonts-cyrillic xfonts-scalable
  fonts-ipafont-gothic fonts-wqy-zenhei fonts-tlwg-loma-otf fonts-freefont-ttf
)

read -r -a APT <<< "${APT_GET:-sudo apt-get}"
read -r -a DPKG <<< "${DPKG_QUERY:-dpkg-query}"
read -r -a PY <<< "${PYTHON:-python}"
read -r -a TIMEOUT <<< "${TIMEOUT_CMD:-timeout}"
OPTIONAL_BUDGET_S="${JT_OPTIONAL_FONT_TIMEOUT_S:-180}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# fail fast instead of apt's default of retrying an unreachable mirror for minutes
APT_NET_OPTS=(-o Acquire::Retries=1 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20)

missing() {
  local p
  for p in "$@"; do
    # shellcheck disable=SC2016  # ${Status} is dpkg-query's field syntax, not a shell variable
    if ! "${DPKG[@]}" -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed"; then
      echo "$p"
    fi
  done
}

# -- 1. required system packages (mandatory) ---------------------------------
mapfile -t need < <(missing "${REQUIRED_PACKAGES[@]}")
if ((${#need[@]})); then
  echo "Installing required Chromium dependencies: ${need[*]}"
  "${APT[@]}" update
  "${APT[@]}" install -y --no-install-recommends "${need[@]}"
  mapfile -t still < <(missing "${need[@]}")
  if ((${#still[@]})); then
    echo "::error title=Chromium dependencies missing::still not installed: ${still[*]}"
    exit 1
  fi
else
  echo "All ${#REQUIRED_PACKAGES[@]} required Chromium dependencies are already installed."
fi

# -- 2. the browser (mandatory) ------------------------------------------------
"${PY[@]}" -m playwright install chromium

# -- 3. optional fonts (best effort) ------------------------------------------
mapfile -t want < <(missing "${OPTIONAL_FONT_PACKAGES[@]}")
if ((${#want[@]})); then
  echo "Installing optional fonts (best effort): ${want[*]}"
  status=0
  # The network part shares one deadline. `apt-get update` exits 0 when every
  # mirror is unreachable (it only warns, then uses stale lists: 3 min 20 s on
  # 2026-11-11); --error-on=any makes that a failure, so the download is
  # skipped instead of retried. The install afterwards reads only the
  # downloaded files.
  deadline=$((SECONDS + OPTIONAL_BUDGET_S))
  "${TIMEOUT[@]}" "$OPTIONAL_BUDGET_S" "${APT[@]}" "${APT_NET_OPTS[@]}" --error-on=any update || status=$?
  if ((status == 0)); then
    left=$((deadline - SECONDS))
    if ((left > 0)); then
      "${TIMEOUT[@]}" "$left" "${APT[@]}" "${APT_NET_OPTS[@]}" install -y --no-install-recommends \
        --download-only "${want[@]}" || status=$?
    else
      status=124
    fi
  fi
  if ((status == 0)); then
    "${APT[@]}" install -y --no-install-recommends "${want[@]}" || status=$?
  fi
  if ((status != 0)); then
    mapfile -t absent < <(missing "${want[@]}")
    echo "::warning title=Optional fonts not installed::apt exited ${status}; continuing without ${absent[*]:-none}. These fonts only affect how non-Latin text is drawn — Chromium's launch is verified next."
  fi
else
  echo "All optional fonts are already installed."
fi

# -- 4. launch check (mandatory) -----------------------------------------------
"${PY[@]}" "$HERE/verify_chromium.py"
