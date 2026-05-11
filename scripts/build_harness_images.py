#!/usr/bin/env python3
"""
Build trust_horizon harness Docker images on top of each hilbench-swe task image.

For each ingested task (read from data/hil_bench_swe/tasks_index.json), this script:
  1. Checks whether the harness image already exists (skips if so)
  2. Runs: docker build --build-arg BASE_IMAGE=<image_name> \\
                        -t <harness_image_prefix>:<uid> \\
                        -f docker/<Dockerfile> .
  from the trust_horizon root directory.

SDK-specific harness images
---------------------------
The base hilbench-swe:<uid> image is always the same (task repo environment).
The harness image is SDK-specific — it bakes in different tooling per agent:

  --sdk claude  (default)
    Image tag:   hilbench-swe-harness-claude:<uid>
    Dockerfile:  docker/Dockerfile.harness
    Bakes in:    Node.js 20, claude CLI, @anthropic-ai/claude-agent-sdk, npm deps

  --sdk codex
    Image tag:   hilbench-swe-harness-codex:<uid>
    Dockerfile:  docker/Dockerfile.harness   (same as claude — Dockerfile.harness installs
                 both @openai/codex and @anthropic-ai/claude-agent-sdk via npm ci)
    Bakes in:    Node.js 20, codex CLI, @openai/codex-sdk, npm deps

Harness source files are NOT baked in for any SDK — they are bind-mounted at
run time, so code changes never require image rebuilds.

Usage:
  python3 scripts/build_harness_images.py                              # all tasks (public+private), claude
  python3 scripts/build_harness_images.py --p-set public              # 100 public tasks only
  python3 scripts/build_harness_images.py --p-set private             # 50 private tasks only
  python3 scripts/build_harness_images.py --sdk claude                # explicit claude
  python3 scripts/build_harness_images.py --sdk codex --p-set public  # codex, public only
  python3 scripts/build_harness_images.py --uids 69bc1094... 69a9... 69c6...
  python3 scripts/build_harness_images.py --workers 4                 # parallel builds
  python3 scripts/build_harness_images.py --force                     # rebuild even if present
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
TASKS_INDEX = ROOT / "data" / "hil_bench_swe" / "tasks_index.json"

# Registry of supported SDKs: sdk_name → (image_tag_prefix, dockerfile_path)
# Add a new entry here when onboarding a new agent SDK.
SDK_REGISTRY: dict[str, tuple[str, Path]] = {
    "claude": (
        "hilbench-swe-harness-claude",
        ROOT / "docker" / "Dockerfile.harness",
    ),
    "codex": (
        "hilbench-swe-harness-codex",
        ROOT / "docker" / "Dockerfile.harness",   # shared; Dockerfile installs both SDKs
    ),
}
DEFAULT_SDK = "claude"

# Kept for import compatibility with run_hil_swe.py
HARNESS_IMAGE_PREFIX = SDK_REGISTRY[DEFAULT_SDK][0]


def docker_image_exists(image_name: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image_name],
        capture_output=True, check=False,
    )
    return result.returncode == 0


def build_harness_image(
    uid: str, base_image: str, force: bool,
    image_prefix: str, dockerfile: Path,
) -> tuple[str, bool, str]:
    """Build a harness image for a single task.  Returns (uid, success, message)."""
    harness_image = f"{image_prefix}:{uid}"

    if not force and docker_image_exists(harness_image):
        return uid, True, f"already exists: {harness_image}"

    if not docker_image_exists(base_image):
        return uid, False, f"base image not found: {base_image} — run ingest_hil_swe.py first"

    if not dockerfile.exists():
        return uid, False, f"Dockerfile not found: {dockerfile}"

    print(f"  [{uid}] Building {harness_image} from {base_image} ...", flush=True)
    cmd = [
        "docker", "build",
        "--build-arg", f"BASE_IMAGE={base_image}",
        "-t", harness_image,
        "-f", str(dockerfile),
        ".",
    ]
    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
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


def _task_is_public(task: dict) -> bool:
    """Return True if a task belongs to the public partition.

    Checks the ``is_public`` field written by ingest_hil_swe.py; falls back to
    inspecting the ``instance_id`` prefix (``public_swe_N`` vs ``private_swe_N``)
    so that existing tasks_index.json files without the field still work.
    """
    if "is_public" in task:
        return bool(task["is_public"])
    return str(task.get("instance_id", "")).startswith("public_")


def filter_tasks_by_pset(tasks: list[dict], p_set: str) -> list[dict]:
    """Filter tasks by partition set.

    p_set values:
      "both"    — all tasks (public + private)
      "public"  — only the 100 public tasks
      "private" — only the 50 private tasks
    """
    if p_set == "both":
        return tasks
    if p_set == "public":
        return [t for t in tasks if _task_is_public(t)]
    # private
    return [t for t in tasks if not _task_is_public(t)]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build trust_horizon harness Docker images for ingested HiL-bench SWE tasks."
    )
    parser.add_argument(
        "--sdk", choices=list(SDK_REGISTRY), default=DEFAULT_SDK,
        help=f"Agent SDK to build harness for (default: {DEFAULT_SDK}). "
             f"Each SDK uses a different Dockerfile and image tag prefix. "
             f"Supported: {', '.join(SDK_REGISTRY)}.",
    )
    parser.add_argument("--uids", nargs="+", metavar="UID",
                        help="Build only for these specific attempt UIDs.")
    parser.add_argument(
        "--p-set", choices=["public", "private", "both"], default="both",
        help=(
            "Partition set to build when --uids is not given (default: both). "
            "'public' = 100 public tasks, 'private' = 50 private tasks, "
            "'both' = all 150 tasks. Ignored when --uids is given."
        ),
    )
    parser.add_argument("--workers", type=int, default=None,
                        help="Parallel docker build workers. Defaults to min(num_tasks, 2). "
                             "Note: docker builds are CPU/IO heavy; keep ≤4 to avoid contention.")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild even if harness image already exists.")
    args = parser.parse_args()

    image_prefix, dockerfile = SDK_REGISTRY[args.sdk]
    print(f"SDK: {args.sdk}  →  image prefix: {image_prefix}  Dockerfile: {dockerfile.name}")

    tasks = load_tasks_index()
    by_uid = {t["uid"]: t for t in tasks}

    if args.uids:
        missing = [u for u in args.uids if u not in by_uid]
        if missing:
            print(f"ERROR: UIDs not in tasks_index: {missing}", file=sys.stderr)
            sys.exit(1)
        target_tasks = [by_uid[u] for u in args.uids]
    else:
        target_tasks = filter_tasks_by_pset(tasks, args.p_set)
        print(f"p-set: {args.p_set}  →  {len(target_tasks)} task(s) selected from {len(tasks)} ingested")

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
            image_prefix=image_prefix,
            dockerfile=dockerfile,
        )

    if workers == 1:
        for task in tqdm(target_tasks, desc="Building", unit="image"):
            uid, ok, msg = build_one(task)
            (successes if ok else failures).append((uid, msg))
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(build_one, t): t["uid"] for t in target_tasks}
            with tqdm(total=len(target_tasks), desc="Building", unit="image") as pbar:
                for future in as_completed(futures):
                    uid, ok, msg = future.result()
                    (successes if ok else failures).append((uid, msg))
                    pbar.update(1)

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
