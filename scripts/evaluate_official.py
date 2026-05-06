#!/usr/bin/env python3
"""Run the official SWE-bench Pro evaluator against normalized predictions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUTONOMY_ROOT = Path(os.environ.get("AUTONOMY_CALIBRATION_ROOT", "/mnt/efs/mohamedelfeki/Codes/autonomy_calibration"))


def default_eval_workers() -> int:
    if os.getenv("SWEBENCH_EVAL_WORKERS"):
        return int(os.environ["SWEBENCH_EVAL_WORKERS"])
    return min(8, max(1, (os.cpu_count() or 1) // 32))


def docker_env() -> dict[str, str]:
    env = dict(os.environ)
    if env.get("DOCKER_HOST"):
        return env
    try:
        raw = subprocess.check_output(["docker", "context", "inspect"], text=True, stderr=subprocess.DEVNULL)
        context = json.loads(raw)[0]
        endpoint = context.get("Endpoints", {}).get("docker", {}).get("Host")
        if endpoint:
            env["DOCKER_HOST"] = endpoint
    except Exception:
        pass
    return env


def clean_existing_outputs(predictions: Path, out_dir: Path) -> None:
    try:
        rows = json.loads(predictions.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Could not read predictions for evaluator cleanup: {predictions}: {exc}") from exc
    for row in rows:
        if not isinstance(row, dict):
            continue
        instance_id = str(row.get("instance_id") or "")
        prefix = str(row.get("prefix") or "")
        if not instance_id or not prefix:
            continue
        attempt_dir = out_dir / instance_id
        for suffix in ("_output.json", "_stdout.log", "_stderr.log", "_entryscript.sh", "_patch.diff"):
            (attempt_dir / f"{prefix}{suffix}").unlink(missing_ok=True)
    (out_dir / "eval_results.json").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--predictions", type=Path, default=None)
    parser.add_argument("--samples", type=Path, default=DEFAULT_AUTONOMY_ROOT / "data" / "swebench_pro_samples.csv")
    parser.add_argument("--vendor", type=Path, default=Path(os.environ.get("SWEBENCH_PRO_VENDOR_DIR", DEFAULT_AUTONOMY_ROOT / "vendor" / "SWE-bench_Pro-os")))
    parser.add_argument("--dockerhub-username", default="jefzda")
    parser.add_argument("--no-local-docker", action="store_true")
    parser.add_argument("--reuse-existing", action="store_true", help="Reuse existing per-attempt evaluator outputs instead of forcing --redo.")
    parser.add_argument("--num-workers", type=int, default=default_eval_workers())
    parser.add_argument("--extra-arg", action="append", default=[])
    args = parser.parse_args()

    run_dir = ROOT / "evals" / args.run_id
    predictions = (args.predictions or run_dir / "predictions.json").resolve()
    samples = args.samples.resolve()
    vendor = args.vendor.resolve()
    evaluator = vendor / "swe_bench_pro_eval.py"
    scripts_dir = vendor / "run_scripts"
    if not evaluator.exists():
        raise SystemExit(f"Missing evaluator: {evaluator}. Run scripts/setup_vendor.py first.")
    if not predictions.exists():
        raise SystemExit(f"Missing predictions: {predictions}")
    if not samples.exists():
        raise SystemExit(f"Missing sample CSV: {samples}")

    out_dir = run_dir / "official-eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.reuse_existing:
        clean_existing_outputs(predictions, out_dir)
    command = [
        sys.executable,
        str(evaluator),
        "--raw_sample_path",
        str(samples),
        "--patch_path",
        str(predictions),
        "--output_dir",
        str(out_dir),
        "--scripts_dir",
        str(scripts_dir),
        "--dockerhub_username",
        args.dockerhub_username,
        "--num_workers",
        str(args.num_workers),
    ]
    if not args.no_local_docker:
        command.append("--use_local_docker")
    if not args.reuse_existing:
        command.append("--redo")
    command.extend(args.extra_arg)
    (out_dir / "command.json").write_text(json.dumps(command, indent=2) + "\n", encoding="utf-8")
    with (out_dir / "stdout.log").open("w", encoding="utf-8") as stdout, (out_dir / "stderr.log").open("w", encoding="utf-8") as stderr:
        process = subprocess.run(command, cwd=vendor, stdout=stdout, stderr=stderr, env=docker_env() if not args.no_local_docker else None)
    print(f"Official evaluator exited with {process.returncode}")
    print(f"Logs: {out_dir}")
    raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
