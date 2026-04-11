#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

python3 tools/youtube/build_guest_video_metadata.py
python3 tools/check/validate_guest_data.py

echo
echo "Next step in lizheng-personal-site:"
echo "  pnpm refresh:guests"
