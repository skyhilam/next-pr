#!/usr/bin/env bash
# Observe canonical exact-SHA PR/CI facts. Read-only; unknown checks never pass.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m next_pr.check_pr "$@"
