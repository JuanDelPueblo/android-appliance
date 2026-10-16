#!/usr/bin/env bash
# lock-deps.sh: pin the Python dependencies with hashes for the image
# (Fedora 44, Python 3.14, x86_64). Needs uv.
set -euo pipefail

cd "$(dirname "$0")/.."
for name in requirements requirements-dev; do
  uv pip compile "$name.in" --quiet --generate-hashes \
    --python-version 3.14 --python-platform x86_64-manylinux_2_28 \
    --output-file "$name.lock"
done
