#!/usr/bin/env bash
# dev-venv.sh: create .venv with the pinned test dependencies and the
# generated gRPC stubs. Needs uv. Prints the path of the Python.
set -euo pipefail

cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  uv venv --quiet --python 3.14 .venv >&2
fi
uv pip sync --quiet --python .venv/bin/python requirements-dev.lock >&2
scripts/gen-protos.sh .venv/bin/python >&2
echo "$PWD/.venv/bin/python"
