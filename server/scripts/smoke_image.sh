#!/usr/bin/env bash
# The Python runner owns a unique project, temporary secrets, dynamic ports, and cleanup.
set -euo pipefail
exec python3 "$(dirname "$0")/run_smoke.py"
