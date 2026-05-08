"""
Host-side orchestrator for trust_horizon HiL-SWE runs.

Runs the full pipeline in one shot:
  Phase 1 — Solve:    spin up harness containers in parallel; each produces patch.diff + trajectory
  Phase 2 — Evaluate: for each completed solve, run the eval container (apply patches, run tests)
  Phase 3 — Metrics:  compute pass@k and ask precision/recall/f1 (micro, like run_hil_bench.py)

All three phases can be individually skipped with --skip-eval / --skip-metrics.

Output layout:
  runs/<run_id>/
    <uid>/
      <mode>/
        pass_<n>/
          attempt.json        harness metadata
          trajectory.jsonl    raw SDK event stream
          trajectory.json     [{act, obs, thought?}, ...]  (SWE-agent compatible format)
          stats.json          {num_steps, num_questions, num_blockers_resolved, ...}
          patch.diff          agent's git diff
          result.json         solve outcome
          eval_result.json    test pass/fail (written by Phase 2)
    metrics/
      pass_level.json
      summary.json

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

  # Solve only (skip eval and metrics), e.g. for a quick pilot
  python3 scripts/run_hil_swe.py --run-id pilot --uids ... --skip-eval --skip-metrics

  # All 100 public tasks
  python3 scripts/run_hil_swe.py --run-id pub100 --all --modes ask_human full_info --passes 3

  # Eval-only on existing solves (result.json already present, want eval_result.json):
  # Solve phase is automatically skipped (result.json exists); eval runs on already-solved passes.
  python3 scripts/run_hil_swe.py --run-id <existing-run-id> --uids ... --modes ... --passes N --skip-metrics

  # Metrics-only on existing evals (eval_result.json already present, want summary.json):
  # Both solve and eval are skipped; metrics are computed from files already on disk.
  python3 scripts/run_hil_swe.py --run-id <existing-run-id> --uids ... --modes ... --passes N --skip-eval

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
    PERMISSION_MODE        claude permissionMode (default: acceptEdits)
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
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

# Allow importing sibling scripts without installation
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from eval_hil_swe import eval_attempt as _eval_attempt  # noqa: E402
from metrics_hil_swe import load_pass_rows, summarize  # noqa: E402

# Shared run-owner directory (mirrors run_hil_bench.py / eval_hil_swe.py).
# A PID token is written here at the start of each run; cleanup helpers check it
# before removing running containers so we never kill a container mid-pass.
RUN_OWNER_DIR = Path(os.getenv("HIL_BENCH_RUN_OWNER_DIR", "/tmp/hil_bench_run_owners"))


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


# ── Run-owner token (mirrors run_hil_bench.py) ──────────────────────────────

def register_run_owner() -> Path:
    """Write a PID token so cleanup helpers know this process is still alive."""
    RUN_OWNER_DIR.mkdir(parents=True, exist_ok=True)
    token = RUN_OWNER_DIR / f"{os.getpid()}.owner"
    token.write_text(str(os.getpid()))
    return token


def unregister_run_owner(token: Path) -> None:
    token.unlink(missing_ok=True)


def any_run_active() -> bool:
    """Return True if any registered run-owner process is still alive."""
    if not RUN_OWNER_DIR.exists():
        return False
    for t in RUN_OWNER_DIR.glob("*.owner"):
        try:
            pid = int(t.stem)
            os.kill(pid, 0)  # 0 = just probe; raises if dead
            return True
        except ProcessLookupError:
            t.unlink(missing_ok=True)
        except (ValueError, PermissionError):
            return True
    return False


def cleanup_orphaned_containers(harness_image: str) -> int:
    """Remove containers that exited or are running with no active owner.

    Mirrors run_hil_bench.py's cleanup_swe_containers_for_image logic:
    - Exited/created containers: always remove (they're already done)
    - Running containers: only remove if no run owner is currently registered
      (guards against removing containers of an ongoing parallel run)
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.ID}}\t{{.Status}}",
             "--filter", f"ancestor={harness_image}"],
            capture_output=True, text=True, check=False,
        )
        active_owner = any_run_active()
        to_remove: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            cid, status = parts[0].strip(), parts[1].lower()
            if status.startswith("exited") or status.startswith("created"):
                to_remove.add(cid)
            elif status.startswith("up") and not active_owner:
                to_remove.add(cid)
        if not to_remove:
            return 0
        subprocess.run(
            ["docker", "rm", "-f", *sorted(to_remove)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return len(to_remove)
    except Exception:
        return 0


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
        # hilbench-swe images have pip.conf pointing to non-existent 127.0.0.1:9876;
        # override so any pip install during solving works. Same fix as yaml config +
        # _DOCKERFILE_INSTANCE_PRECONFIGURED in custom_eval.py.
        "-e", "PIP_INDEX_URL=https://pypi.org/simple/",
        # Prevent git/less/man from opening interactive pagers in non-TTY containers.
        # Matches ask_config_claude_opus_4-6.yaml env_variables exactly.
        "-e", "GIT_PAGER=cat",
        "-e", "PAGER=cat",
        "-e", "MANPAGER=cat",
        "-e", "LESS=-R",
        "-e", "LANG=C.UTF-8",
        "-e", "LC_ALL=C.UTF-8",
        "-e", "TQDM_DISABLE=1",
        "-e", "PIP_PROGRESS_BAR=off",
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
    # ── Phase control ──────────────────────────────────────────────────────────
    parser.add_argument(
        "--skip-eval", action="store_true",
        help="Skip Phase 2 (evaluation). Useful when you just want solve output.",
    )
    parser.add_argument(
        "--skip-metrics", action="store_true",
        help="Skip Phase 3 (metrics). Useful when eval is skipped or you compute metrics separately.",
    )
    parser.add_argument(
        "--eval-workers", type=int, default=None,
        help="Max concurrent eval containers (default: same as --workers).",
    )
    parser.add_argument(
        "--eval-timeout", type=int, default=600,
        help="Per-attempt eval timeout in seconds (default: 600).",
    )
    parser.add_argument(
        "--max-turns", type=int, default=None,
        help="Max agent turns per attempt (default: 80, set in run_claude.mjs). "
             "Equivalent to passing --env MAX_TURNS=N.",
    )
    parser.add_argument(
        "--include-partial",
        action="store_true",
        default=False,
        help=(
            "Include attempts with fewer than --passes valid passes in pass@k metrics. "
            "Default False (canonical run_hil_bench.py behaviour): only attempts that "
            "completed ALL expected passes are counted in the pass@k denominators."
        ),
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

    # 4. --max-turns shorthand (equivalent to --env MAX_TURNS=N)
    if args.max_turns is not None:
        effective_env["MAX_TURNS"] = str(args.max_turns)

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

    # Build the set of ALL (uid, mode, pass) keys for this run so we can
    # also evaluate passes that were already solved in a previous invocation.
    all_pass_keys: set[tuple[str, str, int]] = {
        (t["uid"], mode, p)
        for t in target_tasks
        for mode in args.modes
        for p in range(1, args.passes + 1)
    }

    solve_jobs = build_job_list(
        target_tasks, args.modes, args.passes, args.run_id, skip_if_complete=not args.force
    )
    total = len(all_pass_keys)
    skipped_solve = total - len(solve_jobs)
    # Protect against 0-worker executor when there are no solve jobs
    workers = max(1, args.workers if args.workers is not None else min(len(solve_jobs) or 1, 8))
    eval_workers_n = max(1, args.eval_workers if args.eval_workers is not None else workers)

    log(f"Run '{args.run_id}': {len(solve_jobs)} solve job(s) to run ({skipped_solve} already complete), "
        f"{workers} solve workers / {eval_workers_n} eval workers")
    log(f"Modes: {args.modes}  Passes: {args.passes}  Tasks: {len(target_tasks)}")

    successes: list[str] = []
    failures:  list[str] = []
    eval_ok: list[str] = []
    eval_fail: list[str] = []

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

    def eval_one(job: dict) -> tuple[bool, str]:
        return _eval_attempt(
            uid=job["uid"],
            mode=job["mode"],
            pass_index=job["pass_index"],
            run_id=args.run_id,
            skip_if_complete=not args.force,
            timeout_s=args.eval_timeout,
        )

    # ── Pipelined Solve → Eval (concurrent) ───────────────────────────────────
    # Each pass is fully independent.  As soon as a solve container exits, its
    # eval container is queued immediately — we don't wait for other passes.
    # Both thread pools run concurrently, bounded by their respective worker limits.
    # Phase 3 (metrics) runs after all evals finish.

    owner_token = register_run_owner()
    try:
        with ExitStack() as stack:
            solve_exec = stack.enter_context(ThreadPoolExecutor(max_workers=workers))
            eval_exec = (
                stack.enter_context(ThreadPoolExecutor(max_workers=eval_workers_n))
                if not args.skip_eval else None
            )

            solve_futures: dict = {solve_exec.submit(run_one, j): j for j in solve_jobs}
            eval_futures: dict = {}
            submitted_eval_keys: set[tuple[str, str, int]] = set()

            # As each solve finishes, immediately queue its eval.
            for sf in as_completed(solve_futures):
                j = solve_futures[sf]
                ok, msg = sf.result()
                (successes if ok else failures).append(msg)
                log(f"  Solve {'✓' if ok else '✗'} {msg}")

                if eval_exec is not None:
                    key = (j["uid"], j["mode"], j["pass_index"])
                    if key not in submitted_eval_keys:
                        ef = eval_exec.submit(eval_one, j)
                        eval_futures[ef] = j
                        submitted_eval_keys.add(key)

            # Also submit evals for any already-solved passes from a previous run
            # that weren't just solved now (e.g. --force was not set and they had
            # result.json already).
            if eval_exec is not None:
                run_dir = RUNS_DIR / args.run_id
                for uid, mode, pass_idx in sorted(all_pass_keys - submitted_eval_keys):
                    pass_dir = run_dir / uid / mode / f"pass_{pass_idx}"
                    if (pass_dir / "result.json").exists():
                        eval_job = {"uid": uid, "mode": mode, "pass_index": pass_idx}
                        ef = eval_exec.submit(eval_one, eval_job)
                        eval_futures[ef] = eval_job
                        submitted_eval_keys.add((uid, mode, pass_idx))

            # Wait for all evals to finish
            for ef in as_completed(eval_futures):
                j2 = eval_futures[ef]
                ok2, msg2 = ef.result()
                (eval_ok if ok2 else eval_fail).append(msg2)
                log(f"  Eval  {'✓' if ok2 else '✗'} {msg2}")

    finally:
        unregister_run_owner(owner_token)
        cleaned = 0
        for task in target_tasks:
            harness_image = f"{HARNESS_IMAGE_PREFIX}:{task['uid']}"
            cleaned += cleanup_orphaned_containers(harness_image)
        if cleaned > 0:
            log(f"Cleaned up {cleaned} orphaned harness container(s)")

    log(f"\n{'='*60}")
    log(f"Solve:  {len(successes)} succeeded, {len(failures)} failed.")
    for msg in failures:
        log(f"  SOLVE FAILED: {msg}", file=sys.stderr)
    if not args.skip_eval:
        log(f"Eval:   {len(eval_ok)} evaluated, {len(eval_fail)} failed.")
        for msg in eval_fail:
            log(f"  EVAL FAILED: {msg}", file=sys.stderr)

    # ── Phase 3: Metrics (after all passes + evals complete) ──────────────────
    if not args.skip_metrics:
        run_dir = RUNS_DIR / args.run_id
        rows = load_pass_rows(run_dir)
        if rows:
            metrics_dir = run_dir / "metrics"
            metrics_dir.mkdir(exist_ok=True)
            (metrics_dir / "pass_level.json").write_text(json.dumps(rows, indent=2))
            summary = {
                "metadata": {
                    "run_id": args.run_id,
                    "num_passes": args.passes,
                    "include_partial": getattr(args, "include_partial", False),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "formula": "micro/global-totals (run_hil_bench.py summarize_rows)",
                },
                "by_mode_agent_model": summarize(
                    rows,
                    expected_passes=args.passes,
                    include_partial=getattr(args, "include_partial", False),
                ),
            }
            (metrics_dir / "summary.json").write_text(json.dumps(summary, indent=2))
            log(f"\nMetrics written to {metrics_dir}")
            for key, m in sorted(summary["by_mode_agent_model"].items()):
                parts = [f"\n  [{key}]"]
                for k in range(1, args.passes + 1):
                    pa = m.get(f"pass_at_{k}")
                    n = m.get(f"pass_at_{k}_n", 0)
                    if pa is not None:
                        parts.append(f"    pass@{k}={pa:.3f} (n={n})")
                if m.get("ask_f1") is not None:
                    q  = m.get("total_questions", 0)
                    qt = m.get("total_total_questions", 0)
                    r  = m.get("total_blockers_resolved", 0)
                    b  = m.get("total_blockers_present", 0)
                    parts.append(
                        f"    ask (judge q={q}):  "
                        f"P={m.get('ask_precision',0):.3f} "
                        f"R={m.get('ask_recall',0):.3f} "
                        f"F1={m.get('ask_f1',0):.3f}  resolved={r}/{b}"
                    )
                    parts.append(
                        f"    ask (total q={qt}): "
                        f"P={m.get('ask_precision_total',0):.3f} "
                        f"R={m.get('ask_recall_total',0):.3f} "
                        f"F1={m.get('ask_f1_total',0):.3f}"
                    )
                log("\n".join(parts))
        else:
            log("\nMetrics: no evaluated data yet.")
    else:
        log("\nMetrics: skipped (--skip-metrics).")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
