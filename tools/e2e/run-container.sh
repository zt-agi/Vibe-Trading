#!/usr/bin/env bash
# ZT add-on: run the UI E2E gate in a Linux container against the fixture server.
#
#   tools/e2e/run-container.sh [--via vt-zt] [playwright test args...]
#
# Rebuilds the SPA (skip with VT_E2E_SKIP_BUILD=1), installs the suite's one
# dependency, then runs Playwright; playwright.config.ts starts
# fixture_server.py itself. Needs Node >= 22.22, a Python with VT's
# dependencies (VT_E2E_PYTHON, default python3) and Playwright's Chromium
# already on disk (PLAYWRIGHT_BROWSERS_PATH): this script never downloads a
# browser. Set VT_E2E_PROJECT_ROOT to a work/Investment-AI-Drive-Research copy
# to test against real project files instead of the small test fixture.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
if [ "${1:-}" = "--via" ]; then
  export VT_E2E_VIA="$2"
  shift 2
fi
if [ "${VT_E2E_VIA:-}" = "vt-zt" ] && [ -z "${VT_ZT_LAUNCHER_DIR:-}" ]; then
  echo "run-container.sh: --via vt-zt needs VT_ZT_LAUNCHER_DIR (the folder holding vt_zt_launcher/)" >&2
  exit 2
fi
if [ "${VT_E2E_SKIP_BUILD:-0}" != "1" ]; then
  (cd "$repo/frontend" && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npm run build)
fi
cd "$here"
[ -d node_modules ] || PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci --no-audit --no-fund
exec npx playwright test "$@"
