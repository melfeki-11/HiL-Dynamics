#!/usr/bin/env python3
"""
Build trust_horizon harness Docker images on top of each hilbench-swe task image.

For each ingested task (read from data/hil_bench_swe/tasks_index.json), this script:
  1. Checks whether hilbench-swe-harness:<uid> already exists (skips if so)
  2. Runs: docker build --build-arg BASE_IMAGE=<image_name> \\
                        -t hilbench-swe-harness:<uid> \\
                        -f docker/Dockerfile.harness .
  from the trust_horizon root directory.

The harness image bakes in:
  - Node.js 20
  - All npm dependencies from package.json (agent SDKs + CLI binaries)
  - Google ADK Python package

Harness source files are NOT baked in — they are bind-mounted at run time,
so code changes never require image rebuilds.

Usage:
  python3 scripts/build_harness_images.py                      # all ingested tasks
  python3 scripts/build_harness_images.py --uids 69bc1094... 69a9... 69c6...
  python3 scripts/build_harness_images.py --workers 4          # parallel builds
  python3 scripts/build_harness_images.py --force              # rebuild even if present
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS_INDEX = ROOT / "data" / "hil_bench_swe" / "tasks_index.json"
DOCKERFILE = ROOT / "docker" / "Dockerfile.harness"

HARNESS_IMAGE_PREFIX = "hilbench-swe-harness"


def docker_image_exists(image_name: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image_name],
        capture_output=True, check=False,
    )
    return result.returncode == 0


def build_harness_image(uid: str, base_image: str, force: bool) -> tuple[str, bool, str]:
    """
    Build harness image for a single task.
    Returns (uid, success, message).
    """
    harness_image = f"{HARNESS_IMAGE_PREFIX}:{uid}"

    if not force and docker_image_exists(harness_image):
        return uid, True, f"already exists: {harness_image}"

    if not docker_image_exists(base_image):
        return uid, False, f"base image not found: {base_image} — run ingest_hil_swe.py first"

    print(f"  [{uid}] Building {harness_image} from {base_image} ...", flush=True)
    cmd = [
        "docker", "build",
        "--build-arg", f"BASE_IMAGE={base_image}",
        "-t", harness_image,
        "-f", str(DOCKERFILE),
        ".",
    ]
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = f"docker build failed:\n{result.stderr[-2000:]}"
        print(f"  [{uid}] ERROR: {msg}", flush=True)
        return uid, False, msg

    print(f"  [{uid}] Built {harness_image} ✓", flush=True)
    return uid, True, f"built: {harness_image}"


def load_tasks_index() -> list[dict]:
    if not TASKS_INDEX.exists():
        print(f"ERROR: {TASKS_INDEX} not found. Run ingest_hil_swe.py first.", file=sys.stderr)
        sys.exit(1)
    return json.loads(TASKS_INDEX.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build trust_horizon harness Docker images for ingested HiL-bench SWE tasks."
    )
    parser.add_argument("--uids", nargs="+", metavar="UID",
                        help="Build only for these specific attempt UIDs.")
    parser.add_argument("--workers", type=int, default=None,
                        help="Parallel docker build workers. Defaults to min(num_tasks, 2). "
                             "Note: docker builds are CPU/IO heavy; keep ≤4 to avoid contention.")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild even if harness image already exists.")
    args = parser.parse_args()

    tasks = load_tasks_index()
    by_uid = {t["uid"]: t for t in tasks}

    if args.uids:
        missing = [u for u in args.uids if u not in by_uid]
        if missing:
            print(f"ERROR: UIDs not in tasks_index: {missing}", file=sys.stderr)
            sys.exit(1)
        target_tasks = [by_uid[u] for u in args.uids]
    else:
        target_tasks = tasks

    workers = args.workers if args.workers is not None else min(len(target_tasks), 2)
    print(f"Building harness images for {len(target_tasks)} task(s) with {workers} worker(s)...\n",
          flush=True)

    successes = []
    failures = []

    def build_one(task: dict) -> tuple[str, bool, str]:
        return build_harness_image(
            uid=task["uid"],
            base_image=task["image_name"],
            force=args.force,
        )

    if workers == 1:
        for task in target_tasks:
            uid, ok, msg = build_one(task)
            (successes if ok else failures).append((uid, msg))
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(build_one, t): t["uid"] for t in target_tasks}
            for future in as_completed(futures):
                uid, ok, msg = future.result()
                (successes if ok else failures).append((uid, msg))

    print(f"\n{'='*60}")
    print(f"Done: {len(successes)} succeeded, {len(failures)} failed.")
    for uid, msg in successes:
        print(f"  ✓ {uid}: {msg}")
    for uid, msg in failures:
        print(f"  ✗ {uid}: {msg}", file=sys.stderr)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
