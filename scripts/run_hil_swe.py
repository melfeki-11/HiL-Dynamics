"""
Host-side orchestrator for trust_horizon HiL-SWE runs.

For each (uid, mode, pass) tuple, starts a hilbench-swe-harness:<uid> Docker container
that runs the claude-code SWE harness entrypoint.  All runs proceed concurrently up to
--workers containers at a time.

Output layout:
  runs/<run_id>/
    <uid>/
      <mode>/
        pass_<n>/
          attempt.json
          trajectory.jsonl
          patch.diff
          result.json

Usage examples:
  # Single task, ask_human mode, 1 pass
  python3 scripts/run_hil_swe.py \\
    --run-id my-first-run \\
    --uids 69bc1094b455a91fa20fb868 \\
    --modes ask_human \\
    --passes 1

  # All 3 test tasks, both modes, 3 passes each, 12 concurrent containers
  python3 scripts/run_hil_swe.py \\
    --run-id test3-k3 \\
    --uids 69bc1094b455a91fa20fb868 69a9e77602049c14d2793bb5 69c60cc7b6a31e9900faa779 \\
    --modes ask_human full_info \\
    --passes 3 \\
    --workers 12

  # All 100 public tasks
  python3 scripts/run_hil_swe.py --run-id pub100 --all --modes ask_human full_info --passes 3

Environment variables (read from host env, forwarded into each container):
  Required:
    ANTHROPIC_AUTH_TOKEN   Claude / LiteLLM API key
    LITELLM_BASE_URL       LiteLLM proxy base URL

  Optional:
    LITELLM_API_KEY        fallback API key
    LITELLM_PROXY_API_KEY  fallback API key
    ASK_HUMAN_BASE_URL     override URL for ask_human vLLM judge
    ASK_HUMAN_MODEL        override ask_human judge model slug
    CLAUDE_MODEL           model slug for the agent (default: claude-sonnet-4-6)
    MAX_TURNS              max agent turns (default: 80)
    ATTEMPT_TIMEOUT_MS     per-attempt timeout in ms (default: 3600000)
    PERMISSION_MODE        claude permissionMode (default: bypassPermissions)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


def load_dotenv(env_file: Path) -> dict[str, str]:
    """Parse a .env file into a dict.  Handles: comments, blank lines, quoted values,
    'export' prefix.  Does NOT expand variable references (no $ substitution needed)."""
    result: dict[str, str] = {}
    try:
        for raw_line in env_file.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            if k:
                result[k] = v
    except OSError:
        pass
    return result


def find_env_file(explicit: str | None = None) -> Path | None:
    """Return the first .env file that exists, checking in priority order."""
    candidates = [
        Path(explicit) if explicit else None,
        ROOT / ".env",
        ROOT.parent / "research_evals" / "hil_bench" / ".env",
    ]
    for p in candidates:
        if p and p.exists():
            return p
    return None

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "hil_bench_swe"
TASKS_INDEX = DATA_DIR / "tasks_index.json"
RUNS_DIR = ROOT / "runs"
SRC_DIR = ROOT / "src"

HARNESS_IMAGE_PREFIX = "hilbench-swe-harness"
ENTRYPOINT = "/opt/trust_horizon/src/hil_swe/run_claude.mjs"

# Env vars forwarded from host (or .env) into each container.
# LiteLLM proxy credentials are the most important; the rest are optional overrides.
FORWARDED_ENV_KEYS = [
    # LiteLLM proxy credentials (loaded from .env if not already in process env)
    "LITELLM_BASE_URL",
    "LITELLM_API_KEY",
    "LITELLM_PROXY_API_KEY",
    # Direct Anthropic API key — used as fallback if LITELLM_* not set
    "ANTHROPIC_AUTH_TOKEN",
    # Ask-human judge overrides (optional — defaults to LITELLM_BASE_URL)
    "ASK_HUMAN_BASE_URL",
    "ASK_HUMAN_MODEL",
    "PAPER_ASK_HUMAN_MODEL",
    # Agent / run parameters (all optional; harness uses built-in defaults)
    "CLAUDE_MODEL",
    "MAX_TURNS",
    "ATTEMPT_TIMEOUT_MS",
    "PERMISSION_MODE",
    # AWS credentials (for Bedrock / Secrets Manager, if used)
    "AWS_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
]

_print_lock = threading.Lock()


def log(msg: str, file=sys.stdout) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with _print_lock:
        print(f"[{ts}] {msg}", file=file, flush=True)


def load_tasks_index() -> list[dict]:
    if not TASKS_INDEX.exists():
        print(f"ERROR: {TASKS_INDEX} not found. Run ingest_hil_swe.py first.", file=sys.stderr)
        sys.exit(1)
    return json.loads(TASKS_INDEX.read_text())


def docker_image_exists(image_name: str) -> bool:
    r = subprocess.run(["docker", "image", "inspect", image_name], capture_output=True, check=False)
    return r.returncode == 0


def output_dir_for(run_id: str, uid: str, mode: str, pass_index: int) -> Path:
    return RUNS_DIR / run_id / uid / mode / f"pass_{pass_index}"


def result_is_complete(out_dir: Path) -> bool:
    return (out_dir / "result.json").exists()


def run_attempt(
    *,
    uid: str,
    image_name: str,
    mode: str,
    pass_index: int,
    run_id: str,
    skip_if_complete: bool,
    extra_env: dict[str, str],
) -> tuple[bool, str]:
    """
    Spin up one Docker container to run the claude-code SWE harness for a single
    (uid, mode, pass_index) attempt.  Returns (success, message).
    """
    out_dir = output_dir_for(run_id, uid, mode, pass_index)

    if skip_if_complete and result_is_complete(out_dir):
        return True, f"[{uid[:12]}|{mode}|p{pass_index}] already complete, skipped"

    harness_image = f"{HARNESS_IMAGE_PREFIX}:{uid}"
    if not docker_image_exists(harness_image):
        return False, (
            f"[{uid[:12]}|{mode}|p{pass_index}] harness image {harness_image} not found "
            f"— run build_harness_images.py first"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    task_dir = DATA_DIR / "tasks" / uid

    # Build docker run command
    env_args: list[str] = []
    for key in FORWARDED_ENV_KEYS:
        val = extra_env.get(key) or os.environ.get(key, "")
        if val:
            env_args += ["-e", f"{key}={val}"]

    # Per-attempt env overrides
    env_args += [
        "-e", f"MODE={mode}",
        "-e", f"PASS_INDEX={pass_index}",
        "-e", f"RUN_ID={run_id}",
        "-e", "TASK_DIR=/task",
        "-e", "OUTPUT_DIR=/output",
        "-e", "CLAUDE_CODE_EXECUTABLE=claude",
    ]

    cmd = [
        "docker", "run", "--rm",
        # Allow container to reach host services (LiteLLM proxy, vLLM judge server).
        # --add-host maps host.docker.internal → host gateway (same pattern as hil-bench
        # configs/swe/ask_config_claude_opus_4-6.yaml).  Clients should use
        # http://host.docker.internal:PORT rather than http://localhost:PORT.
        "--add-host=host.docker.internal:host-gateway",
        # task data (read-only)
        "-v", f"{task_dir.resolve()}:/task:ro",
        # harness source (read-only) — changes don't need image rebuilds
        "-v", f"{SRC_DIR.resolve()}:/opt/trust_horizon/src:ro",
        # output (read-write)
        "-v", f"{out_dir.resolve()}:/output",
        *env_args,
        harness_image,
        "node", ENTRYPOINT,
    ]

    label = f"[{uid[:12]}|{mode}|p{pass_index}]"
    log(f"{label} Starting container {harness_image}")

    container_log = out_dir / "container.log"
    started_at = time.time()
    try:
        with open(container_log, "w") as log_fh:
            result = subprocess.run(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                timeout=int(os.environ.get("ATTEMPT_TIMEOUT_MS", "3600000")) // 1000 + 120,
            )
        elapsed = int(time.time() - started_at)
        if result.returncode == 0:
            log(f"{label} Done in {elapsed}s ✓")
            return True, f"{label} done in {elapsed}s"
        else:
            tail = _tail(container_log, 20)
            msg = f"{label} Container exited {result.returncode} after {elapsed}s. Last lines:\n{tail}"
            log(msg, file=sys.stderr)
            return False, msg
    except subprocess.TimeoutExpired:
        msg = f"{label} Timed out on host after {int(time.time() - started_at)}s"
        log(msg, file=sys.stderr)
        return False, msg
    except Exception as exc:
        msg = f"{label} Exception: {exc}"
        log(msg, file=sys.stderr)
        return False, msg


def _tail(path: Path, n: int) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def build_job_list(
    tasks: list[dict],
    modes: list[str],
    passes: int,
    run_id: str,
    skip_if_complete: bool,
) -> list[dict]:
    jobs = []
    for task in tasks:
        for mode in modes:
            for pass_idx in range(1, passes + 1):
                out_dir = output_dir_for(run_id, task["uid"], mode, pass_idx)
                if skip_if_complete and result_is_complete(out_dir):
                    continue
                jobs.append({
                    "uid": task["uid"],
                    "image_name": task["image_name"],
                    "mode": mode,
                    "pass_index": pass_idx,
                })
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run trust_horizon HiL-SWE attempts via Docker containers."
    )
    parser.add_argument("--run-id", required=True, help="Unique run identifier (used as output directory name).")
    uid_group = parser.add_mutually_exclusive_group(required=True)
    uid_group.add_argument("--uids", nargs="+", metavar="UID", help="Attempt UIDs to run.")
    uid_group.add_argument("--all", action="store_true", help="Run all ingested tasks from tasks_index.json.")
    parser.add_argument(
        "--modes", nargs="+", choices=["ask_human", "full_info"], default=["ask_human"],
        help="Modes to run (default: ask_human).",
    )
    parser.add_argument(
        "--passes", "-k", type=int, default=1,
        help="Number of passes per (task, mode) (default: 1).",
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=None,
        help="Max concurrent Docker containers. Defaults to min(num_jobs, 8).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-run even if result.json already exists for an attempt.",
    )
    parser.add_argument(
        "--env-file", metavar="PATH",
        help=(
            "Path to a .env file with LiteLLM credentials "
            "(default: auto-discovers trust_horizon/.env or hil_bench/.env)."
        ),
    )
    parser.add_argument(
        "--env", nargs="*", metavar="KEY=VALUE",
        help="Additional env var overrides to pass into containers (e.g. CLAUDE_MODEL=claude-opus-4-7).",
    )
    args = parser.parse_args()

    # ── Load credentials (.env file → os.environ fallback → explicit --env overrides) ──

    # 1. Find and load the .env file; merge into a working env dict
    env_file = find_env_file(args.env_file)
    dotenv: dict[str, str] = {}
    if env_file:
        dotenv = load_dotenv(env_file)
        log(f"Loaded env from {env_file} ({len(dotenv)} keys)")
    else:
        log("WARNING: No .env file found. Set LITELLM_BASE_URL and LITELLM_API_KEY in the environment.", file=sys.stderr)

    # 2. Build the effective env: .env values fill in gaps in os.environ
    effective_env: dict[str, str] = {}
    for k in FORWARDED_ENV_KEYS:
        val = os.environ.get(k) or dotenv.get(k, "")
        if val:
            effective_env[k] = val

    # 3. Explicit --env KEY=VALUE overrides win over everything
    for item in args.env or []:
        if "=" in item:
            k, v = item.split("=", 1)
            effective_env[k] = v

    # 4. Validate the minimum required vars
    api_key = (
        effective_env.get("LITELLM_API_KEY") or
        effective_env.get("LITELLM_PROXY_API_KEY") or
        effective_env.get("ANTHROPIC_AUTH_TOKEN")
    )
    base_url = effective_env.get("LITELLM_BASE_URL") or effective_env.get("ANTHROPIC_BASE_URL")

    if not api_key:
        print(
            "ERROR: No API key found.  Set LITELLM_API_KEY in trust_horizon/.env "
            "(or ANTHROPIC_AUTH_TOKEN for direct Anthropic access).",
            file=sys.stderr,
        )
        sys.exit(1)
    if not base_url:
        print(
            "ERROR: No base URL found.  Set LITELLM_BASE_URL in trust_horizon/.env "
            "(e.g. https://litellm-proxy.ml-serving-internal.scale.com).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Surface the model being used early so it's visible in logs
    model = effective_env.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    log(f"Proxy: {base_url}  Model: {model}")

    tasks = load_tasks_index()
    by_uid = {t["uid"]: t for t in tasks}

    if args.all:
        target_tasks = tasks
    else:
        missing = [u for u in args.uids if u not in by_uid]
        if missing:
            print(f"ERROR: UIDs not in tasks_index: {missing}", file=sys.stderr)
            sys.exit(1)
        target_tasks = [by_uid[u] for u in args.uids]

    jobs = build_job_list(target_tasks, args.modes, args.passes, args.run_id, skip_if_complete=not args.force)
    total = len(target_tasks) * len(args.modes) * args.passes
    skipped = total - len(jobs)
    workers = args.workers if args.workers is not None else min(len(jobs), 8)

    log(f"Run '{args.run_id}': {len(jobs)} job(s) to run ({skipped} already complete), {workers} workers")
    log(f"Modes: {args.modes}  Passes: {args.passes}  Tasks: {len(target_tasks)}")

    if not jobs:
        log("Nothing to do.")
        return

    successes: list[str] = []
    failures:  list[str] = []

    def run_one(job: dict) -> tuple[bool, str]:
        return run_attempt(
            uid=job["uid"],
            image_name=job["image_name"],
            mode=job["mode"],
            pass_index=job["pass_index"],
            run_id=args.run_id,
            skip_if_complete=not args.force,
            extra_env=effective_env,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_one, j): j for j in jobs}
        for future in as_completed(futures):
            ok, msg = future.result()
            (successes if ok else failures).append(msg)

    log(f"\n{'='*60}")
    log(f"Done: {len(successes)} succeeded, {len(failures)} failed.")
    for msg in failures:
        log(f"  FAILED: {msg}", file=sys.stderr)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
