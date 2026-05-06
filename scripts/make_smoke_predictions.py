#!/usr/bin/env python3
"""Create gold/noop predictions for evaluator and metrics smoke tests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", choices=["gold", "noop"], required=True)
    parser.add_argument("--samples", type=Path, default=ROOT / "data" / "swebench_pro_samples.jsonl")
    parser.add_argument("--limit", type=int, default=1)
    args = parser.parse_args()
    run_dir = ROOT / "evals" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    predictions = []
    for idx, row in enumerate(load_rows(args.samples)[: args.limit], 1):
        patch = row.get("patch") or "" if args.mode == "gold" else ""
        predictions.append({"instance_id": row["instance_id"], "patch": patch, "prefix": f"{args.run_id}__smoke__attempt-{idx}", "harness": "smoke", "attempt_index": idx, "run_id": args.run_id})
    (run_dir / "predictions.json").write_text(json.dumps(predictions, indent=2) + "\n", encoding="utf-8")
    print(run_dir / "predictions.json")


if __name__ == "__main__":
    main()
