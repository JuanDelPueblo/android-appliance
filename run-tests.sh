#!/usr/bin/env bash
# run-tests.sh: run every test suite that needs no emulator.
#
# It runs the bash tests, the Hermes plugin tests and the dashboard
# tests, and reports a clear SKIP when a tool is not installed.
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
  run "shellcheck" shellcheck -x \
    src/androidctl src/avd-init src/appliance-env src/emulator-launch src/display-scrcpy \
    install.sh scripts/provision-sdk.sh tests/*.sh tests/fakes/*
else
  echo "SKIP shellcheck (not installed)"
fi

for suite in androidctl-test.sh avd-init-test.sh appliance-env-test.sh units-test.sh \
  provision-sdk-test.sh install-test.sh; do
  run "$suite" bash "tests/$suite"
done

if command -v python3 >/dev/null 2>&1; then
  python3 -m py_compile src/display-idle
  report "display-idle compiles" $?
  if python3 -c 'import aiohttp, fastapi, httpx, yaml' 2>/dev/null; then
    run "test_plugin.py" python3 -m unittest discover -s tests -v
  else
    echo "SKIP test_plugin.py (python modules missing; on Fedora run: dnf install python3-aiohttp python3-fastapi python3-httpx python3-pyyaml)"
  fi
else
  echo "SKIP display-idle and test_plugin.py (python3 not installed)"
fi

if command -v node >/dev/null 2>&1; then
  node --check dashboard/dist/index.js &&
    node --test tests/dashboard-test.js
  report "dashboard-test.js" $?
else
  echo "SKIP dashboard-test.js (node not installed)"
fi

exit "$status"
