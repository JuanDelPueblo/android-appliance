#!/usr/bin/env bash
# gen-protos.sh: generate the Python gRPC stubs of the emulator protos.
#
# Usage: scripts/gen-protos.sh [PYTHON] [OUT_DIR]
#
# PYTHON must have grpcio-tools (default: python3). OUT_DIR defaults to
# appliance/_proto. The protos import each other by bare file names, so
# the stubs use flat imports such as "import ice_config_pb2". The
# package __init__ puts OUT_DIR on sys.path to make those imports work.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
python=${1:-python3}
out=${2:-$here/appliance/_proto}
protos=$here/appliance/proto

mkdir -p "$out"
"$python" -m grpc_tools.protoc \
  -I"$protos" \
  --python_out="$out" \
  --grpc_python_out="$out" \
  "$protos"/*.proto
cat >"$out/__init__.py" <<'EOF'
"""Generated emulator gRPC stubs (scripts/gen-protos.sh)."""

import os
import sys

# The generated modules import each other by bare names.
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
EOF
