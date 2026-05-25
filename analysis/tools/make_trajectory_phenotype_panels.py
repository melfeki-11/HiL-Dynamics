#!/usr/bin/env python3
"""Generate trajectory action phenotype panels from cached release CSVs."""

from __future__ import annotations

import argparse
from pathlib import Path

import make_release_assets as release_assets


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate per-model-family trajectory action phenotype panels from release CSVs.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=release_assets.OUT_DIR,
        help="Directory containing data/summary_by_group.csv and data/per_run_features.csv; figures are written under figures/.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=120,
        help="Number of turns to plot per trajectory panel.",
    )
    parser.add_argument(
        "--include-open",
        action="store_true",
        help="Include open/single-scaffold model families such as GLM/OpenCode.",
    )
    parser.add_argument(
        "--active-normalized",
        action="store_true",
        help="Emit the older context style normalized over trajectories still active at each turn.",
    )
    parser.add_argument(
        "--include-idle-end",
        action="store_true",
        help="Deprecated alias for the default behavior: include ended trajectories as a top-stacked IDLE/END cap.",
    )
    args = parser.parse_args()
    release_assets.configure_paths(
        argparse.Namespace(
            out_dir=args.out_dir,
            native_runs_root=None,
            swe_agent_raw_root=None,
            swe_agent_analysis_root=None,
            harbor_root=[],
            scrub_local_paths=False,
        )
    )

    rows_path = release_assets.DATA_DIR / "per_run_features.csv"
    summaries_path = release_assets.DATA_DIR / "summary_by_group.csv"
    if not rows_path.exists() or not summaries_path.exists():
        raise SystemExit(f"Missing cached CSVs under {release_assets.DATA_DIR}. Run make_release_assets.py first.")

    rows = release_assets.read_csv_rows(rows_path)
    summaries = release_assets.read_csv_rows(summaries_path)
    include_idle_end = not args.active_normalized or args.include_idle_end
    action_rows = release_assets.trajectory_action_phenotypes_by_turn(
        rows,
        summaries,
        max_turns=args.max_turns,
        include_open=args.include_open,
        include_idle_end=include_idle_end,
    )
    data_name = "trajectory_action_phenotypes_by_turn.csv" if include_idle_end else "trajectory_action_phenotypes_by_turn_active_normalized.csv"
    release_assets.write_csv(release_assets.DATA_DIR / data_name, action_rows)
    suffix = "" if include_idle_end else "_active_normalized"
    paths: list[Path] = release_assets.plot_trajectory_action_phenotype_families(
        action_rows,
        summaries,
        include_open=args.include_open,
        include_idle_end=include_idle_end,
        file_suffix=suffix,
    )

    print(f"Wrote {len(action_rows)} turn/action rows")
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
