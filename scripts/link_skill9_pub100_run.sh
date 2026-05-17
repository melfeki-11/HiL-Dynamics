#!/usr/bin/env bash
# Symlink-merge 20-UID + 80-UID Skill9 runs into pub100 run dirs for metrics.
set -euo pipefail
cd "$(dirname "$0")/.."

TWENTY="data/hil_swe_20_attempt_test_set_uids.txt"
EIGHTY="data/hil_swe_80_remaining_public_uids.txt"

link_sdk() {
  local sdk="$1"
  local run="runs/_swe_skill9_pub100_${sdk}"
  rm -rf "$run"
  mkdir -p "$run"
  while IFS= read -r uid; do
    [[ -z "$uid" || "$uid" =~ ^# ]] && continue
    src20="runs/_swe_skill9_full_${sdk}/${uid}"
    src80="runs/_swe_skill9_pub80_${sdk}/${uid}"
    if [[ -d "$src20" ]]; then
      ln -sfn "../_swe_skill9_full_${sdk}/${uid}" "${run}/${uid}"
    elif [[ -d "$src80" ]]; then
      ln -sfn "../_swe_skill9_pub80_${sdk}/${uid}" "${run}/${uid}"
    else
      echo "WARN: missing uid $uid for $sdk" >&2
    fi
  done < <(cat "$TWENTY" "$EIGHTY")
  echo "Linked $(find "$run" -maxdepth 1 -type l | wc -l) UIDs -> $run"
}

link_sdk claude
link_sdk codex
python3 scripts/metrics_hil_swe.py --run-id "_swe_skill9_pub100_codex" --passes 3 --print
python3 scripts/metrics_hil_swe.py --run-id "_swe_skill9_pub100_claude" --passes 3 --print
