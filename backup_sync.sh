#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Use the Python helper script to handle Windows long paths safely
if command -v python >/dev/null 2>&1; then
  python backup_sync_helper.py
elif command -v py >/dev/null 2>&1; then
  py -3 backup_sync_helper.py
else
  echo "backup_sync.sh: python is required" >&2
  exit 1
fi