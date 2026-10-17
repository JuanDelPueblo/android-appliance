#!/usr/bin/env bash
# run-tests.sh: run every test suite that needs no emulator.
#
# It runs ShellCheck, the bash tests of install.sh and androidctl, the
# Python tests (in the .venv of scripts/dev-venv.sh) and the tests of the
# web dashboard (web/), and reports a clear SKIP when a tool is not
# installed.
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd)
cd "$here" || exit 1
status=0

report() {
  if [ "$2" = 0 ]; then
    echo "PASS $1"
  else
    echo "FAIL $1"
    status=1
  fi
}

run() {
  local name=$1
  shift
  if "$@"; then
    echo "PASS $name"
  else
    echo "FAIL $name"
    status=1
  fi
}

if command -v shellcheck >/dev/null 2>&1; then
  scripts=(bin/androidctl install.sh scripts/*.sh tests/*.sh tests/e2e/*.sh)
  for file in tests/fakes/*; do
    case $file in *.py) ;; *) scripts+=("$file") ;; esac
  done
  run "shellcheck" shellcheck -x "${scripts[@]}"
else
  echo "SKIP shellcheck (not installed)"
fi

for suite in tests/*-test.sh; do
  if [ "$suite" = tests/androidctl-v2-test.sh ] && ! command -v python3 >/dev/null 2>&1; then
    echo "SKIP $suite (python3 runs its fake appliance API)"
    continue
  fi
  run "$suite" bash "$suite"
done

if [ ! -d tests/python ]; then
  echo "SKIP tests/python (the directory is missing)"
elif command -v uv >/dev/null 2>&1; then
  if python=$(scripts/dev-venv.sh); then
    run "tests/python" "$python" -m pytest -q tests/python
  else
    report "tests/python (scripts/dev-venv.sh failed)" 1
  fi
else
  echo "SKIP tests/python (uv not installed; it creates the test environment)"
fi

# The web dashboard: unit tests of its modules, and UI tests in Chromium
# when Playwright is available. It is not a dependency of the repository:
# point AA_PLAYWRIGHT at a node_modules directory that has `playwright`
# (with its Chromium installed), or install it where Node finds it. One of
# the UI suites runs the real daemon against the fake emulator, in the
# Python environment of the tests (it skips itself without one).
if command -v node >/dev/null 2>&1; then
  run "tests/web unit" node --test tests/web/unit/*.test.mjs
  # The real-device test of the dashboard needs KVM and Podman: at least check that it parses.
  syntax=0
  for file in tests/e2e/*.mjs; do node --check "$file" || syntax=1; done
  report "tests/e2e syntax" "$syntax"
  if reason=$(node tests/web/has-playwright.mjs); then
    run "tests/web ui" env AA_PYTHON="${python:-}" node --test tests/web/ui*.test.mjs tests/web/daemon.test.mjs
  else
    echo "SKIP tests/web ui ($reason)"
  fi
else
  echo "SKIP tests/web (node not installed)"
fi

exit "$status"
