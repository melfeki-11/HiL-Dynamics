#!/usr/bin/env python3
"""Run the official HiL-Bench custom SWE evaluator for Trust Horizon predictions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HIL_BENCH_ROOT = ROOT.parent / "hil-bench"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_predictions(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def prediction_model_name(prediction: dict[str, Any]) -> str:
    harness = str(prediction.get("harness") or "unknown")
    model = str(prediction.get("model") or harness)
    mode = str(prediction.get("mode") or "unknown")
    return f"{harness}:{model}:{mode}"


def safe_eval_model_name(prediction: dict[str, Any]) -> str:
    harness = str(prediction.get("harness") or "unknown")
    model = str(prediction.get("model") or harness)
    return f"{harness}_{model}".replace("/", "_").replace(":", "_").replace("-", "_")


def eval_instance_id(prediction: dict[str, Any]) -> str:
    """Return an upstream-compatible unique attempt id.

    HiL-Bench's custom evaluator strips ids of the form
    {original}__{model}__{mode}__pass_{n} back to {original} for metadata
    lookup while preserving the full id in result keys. Trust Horizon prefixes
    put the run/harness first, which the upstream stripper cannot reverse.
    """

    instance_id = str(prediction["instance_id"])
    mode = str(prediction.get("mode") or "unknown").replace("-", "_")
    attempt_index = int(prediction.get("attempt_index") or prediction.get("pass_num") or 1)
    return f"{instance_id}__{safe_eval_model_name(prediction)}__{mode}__pass_{attempt_index}"


def merge_eval_results(parts: list[dict[str, Any]]) -> dict[str, Any]:
    merged_results: dict[str, Any] = {}
    resolved_ids: set[str] = set()
    error_ids: set[str] = set()
    total_instances = 0
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key, value in (part.get("results") or {}).items():
            merged_results[str(key)] = value
        resolved_ids.update(str(item) for item in (part.get("resolved_ids") or []))
        error_ids.update(str(item) for item in (part.get("error_ids") or []))
        total_instances += int(part.get("total_instances") or len(part.get("results") or {}))
    return {
        "resolved_ids": sorted(resolved_ids),
        "error_ids": sorted(error_ids),
        "resolved_instances": len(resolved_ids),
        "results": dict(sorted(merged_results.items())),
        "total_instances": total_instances,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--predictions", type=Path, default=None)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--hil-bench-root", type=Path, default=Path(os.environ.get("HIL_BENCH_ROOT", DEFAULT_HIL_BENCH_ROOT)))
    parser.add_argument("--num-workers", type=int, default=int(os.environ.get("HIL_EVAL_WORKERS", "1")))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("HIL_EVAL_TIMEOUT", "1800")))
    parser.add_argument("--force-rebuild", action="store_true")
    args = parser.parse_args()

    hil_root = args.hil_bench_root.resolve()
    sys.path.insert(0, str(hil_root))
    from hil_bench.utils.custom_eval import evaluate_custom_instances

    run_dir = ROOT / "evals" / args.run_id
    predictions_path = (args.predictions or run_dir / "predictions.json").resolve()
    predictions = load_predictions(predictions_path)
    prefix_to_eval_id: dict[str, str] = {}
    mapped = {
        eval_instance_id(prediction): {
            "instance_id": eval_instance_id(prediction),
            "model_name_or_path": prediction_model_name(prediction),
            "model_patch": str(prediction.get("patch") or ""),
        }
        for prediction in predictions
        if prediction.get("prefix")
    }
    for prediction in predictions:
        if prediction.get("prefix"):
            prefix_to_eval_id[str(prediction["prefix"])] = eval_instance_id(prediction)
    by_original_instance: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for prediction in predictions:
        if prediction.get("prefix"):
            eval_id = eval_instance_id(prediction)
            by_original_instance[str(prediction["instance_id"])][eval_id] = mapped[eval_id]
    out_dir = run_dir / "official-hil-eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out_dir / "preds.json", json.dumps(mapped, indent=2, sort_keys=True) + "\n")
    atomic_write_text(out_dir / "prefix_to_eval_id.json", json.dumps(prefix_to_eval_id, indent=2, sort_keys=True) + "\n")
    if args.num_workers > 1 and any(len(group) > 1 for group in by_original_instance.values()):
        parts: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=args.num_workers) as executor:
            futures = {
                executor.submit(
                    evaluate_custom_instances,
                    predictions=group,
                    tasks_dir=args.tasks_dir.resolve(),
                    run_id=f"trust-horizon.{args.run_id}",
                    timeout=args.timeout,
                    force_rebuild=args.force_rebuild,
                    max_workers=1,
                ): instance_id
                for instance_id, group in by_original_instance.items()
            }
            for future in as_completed(futures):
                parts.append(future.result())
        results = merge_eval_results(parts)
    else:
        results = evaluate_custom_instances(
            predictions=mapped,
            tasks_dir=args.tasks_dir.resolve(),
            run_id=f"trust-horizon.{args.run_id}",
            timeout=args.timeout,
            force_rebuild=args.force_rebuild,
            max_workers=args.num_workers,
        )
    resolved = set(results.get("resolved_ids") or [])
    by_prefix = {prefix: eval_id in resolved for prefix, eval_id in prefix_to_eval_id.items()}
    atomic_write_text(out_dir / "eval_results.json", json.dumps(results, indent=2, sort_keys=True) + "\n")
    atomic_write_text(out_dir / "results_by_prefix.json", json.dumps(by_prefix, indent=2, sort_keys=True) + "\n")
    print(out_dir / "eval_results.json")


if __name__ == "__main__":
    main()
