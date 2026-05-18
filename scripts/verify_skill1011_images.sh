#!/usr/bin/env bash
# Verify 20-UID harness images exist for all four SDK prefixes.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/skill1011_common.sh
source scripts/skill1011_common.sh
skill1011_load_uids

PREFIXES=(
  "hilbench-swe-harness-claude"
  "hilbench-swe-harness-codex"
  "hilbench-swe-harness-adk"
  "hilbench-swe-harness-opencode"
)

missing=0
for prefix in "${PREFIXES[@]}"; do
  for uid in "${UIDS[@]}"; do
    if ! docker image inspect "${prefix}:${uid}" >/dev/null 2>&1; then
      echo "MISSING ${prefix}:${uid}"
      missing=$((missing + 1))
    fi
  done
done

if [[ "$missing" -gt 0 ]]; then
  echo "Missing $missing image(s). Build with e.g.:"
  echo "  python3 scripts/build_harness_images.py --uids <uid> --sdk claude"
  exit 1
fi
echo "OK: all ${#UIDS[@]} UIDs × ${#PREFIXES[@]} SDK harness images present"
