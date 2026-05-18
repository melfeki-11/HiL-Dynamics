#!/usr/bin/env python3
"""List UIDs that lack metric-eligible passes for a Skill10/11 run (20/20 gate)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.metrics_hil_swe import (  # noqa: E402
    STATS_SCHEMA_VERSION,
    _trajectory_needs_rerun,
    load_pass_rows,
    pass_has_valid_stats_schema,
)

DEFAULT_UIDS = ROOT / "data" / "hil_swe_20_attempt_test_set_uids.txt"
RUNS_DIR = ROOT / "runs"


def _load_expected_uids(path: Path) -> list[str]:
    uids: list[str] = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            uids.append(line)
    return uids


def _row_is_valid_pass(row: dict) -> bool:
    if row.get("mode") != "ask_human":
        return False
    if row.get("status") == "infra_error":
        return False
    pass_dir = row.get("pass_dir") or ""
    if _trajectory_needs_rerun(pass_dir):
        return False
    if not pass_has_valid_stats_schema(pass_dir):
        return False
    return True


def audit_run(
    run_id: str,
    *,
    uids_file: Path,
    expected_passes: int,
    mode: str,
) -> dict:
    expected_uids = _load_expected_uids(uids_file)
    run_dir = RUNS_DIR / run_id
    rows = load_pass_rows(run_dir) if run_dir.exists() else []

    by_uid: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("mode") != mode:
            continue
        by_uid.setdefault(row["uid"], []).append(row)

    per_uid: dict[str, dict] = {}
    included = 0
    incomplete_uids: list[str] = []

    for uid in expected_uids:
        uid_rows = sorted(by_uid.get(uid, []), key=lambda r: r["pass_index"])
        valid_indices: list[int] = []
        invalid_reasons: list[str] = []
        for row in uid_rows:
            if _row_is_valid_pass(row):
                valid_indices.append(int(row["pass_index"]))
            else:
                reason = row.get("status", "unknown")
                pass_dir = row.get("pass_dir") or ""
                if pass_dir and not pass_has_valid_stats_schema(pass_dir):
                    reason = "stats_schema_not_v2"
                elif pass_dir and _trajectory_needs_rerun(pass_dir):
                    reason = "trajectory_needs_rerun"
                invalid_reasons.append(f"p{row['pass_index']}:{reason}")

        valid_n = len(valid_indices)
        missing_passes = [
            p for p in range(1, expected_passes + 1) if p not in valid_indices
        ]
        per_uid[uid] = {
            "valid": valid_n,
            "valid_pass_indices": valid_indices,
            "missing_passes": missing_passes,
            "invalid_reasons": invalid_reasons,
        }
        if valid_n >= expected_passes:
            included += 1
        else:
            incomplete_uids.append(uid)

    return {
        "run_id": run_id,
        "included": included,
        "expected": len(expected_uids),
        "incomplete_uids": incomplete_uids,
        "per_uid": per_uid,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--uids-file", type=Path, default=DEFAULT_UIDS)
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--mode", default="ask_human")
    parser.add_argument("--print", dest="print_line", action="store_true")
    parser.add_argument(
        "--query",
        choices=["incomplete_uids", "included", "expected"],
        help="Print a single field (space-separated list for incomplete_uids).",
    )
    parser.add_argument("--json", action="store_true", help="Print full audit JSON.")
    args = parser.parse_args()

    report = audit_run(
        args.run_id,
        uids_file=args.uids_file,
        expected_passes=args.passes,
        mode=args.mode,
    )

    if args.query:
        val = report[args.query]
        if args.query == "incomplete_uids":
            print(" ".join(val))
        else:
            print(val)
    elif args.json or not args.print_line:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"included={report['included']}/{report['expected']} "
            f"incomplete={len(report['incomplete_uids'])}"
        )

    if report["included"] != report["expected"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
