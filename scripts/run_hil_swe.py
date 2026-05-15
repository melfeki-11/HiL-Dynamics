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

  # All 100 public tasks, or all 150 (default --p-set both)
  python3 scripts/run_hil_swe.py --run-id pub100 --p-set public --modes ask_human full_info --passes 3
  python3 scripts/run_hil_swe.py --run-id all150 --p-set both --modes ask_human --passes 1

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
    LITELLM_API_KEY             fallback API key
    LITELLM_PROXY_API_KEY       fallback API key
    ASK_HUMAN_BASE_URL          override URL for ask_human LiteLLM judge
    ASK_HUMAN_MODEL             override ask_human judge model slug
    CLAUDE_MODEL                model slug for the agent when --sdk claude (default: claude-opus-4-7)
    CODEX_MODEL                 model slug for the agent when --sdk codex  (default: gpt-5.5)
    ADK_MODEL                   model slug for the agent when --sdk adk    (default: gemini/gemini-3.1-pro-preview-customtools)
    OPENCODE_MODEL              model slug for the agent when --sdk opencode (default: fireworks_ai/glm-5p1)
    CLAUDE_REASONING_EFFORT     reasoning effort for Claude SDK query options (low|medium|high|xhigh)
    CODEX_REASONING_EFFORT      reasoning effort for Codex app-server (none|minimal|low|medium|high|xhigh)
    ADK_REASONING_EFFORT        best-effort reasoning effort forwarded to LiteLLM (low|medium|high)
    OPENCODE_REASONING_EFFORT   reasoning effort for OpenCode provider config (low|medium|high|xhigh|max)
    OPENCODE_STARTUP_TIMEOUT_MS startup watchdog before first OpenCode stdout event (default: 300000)
    LITELLM_CALL_TIMEOUT_MS     per-LiteLLM-call timeout in ms (default: 1200000 / 20 min)
    STEP_LITELLM_TRIES          retries per agent step/call budget (default: 3)
    MAX_TURNS                   max agent turns (default: 200)
    ATTEMPT_TIMEOUT_MS          per-attempt timeout in ms (default: 10800000)
    PERMISSION_MODE             claude permissionMode (default: acceptEdits)
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Allow importing sibling scripts without installation
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from eval_hil_swe import eval_attempt as _eval_attempt, cleanup_orphaned_eval_containers  # noqa: E402
from metrics_hil_swe import load_pass_rows, summarize  # noqa: E402

# Per-uid owner directory (mirrors paper_pipeline.py ATTEMPT_OWNER_DIR pattern).
# Each run_attempt() call writes a "{uid}__{pid}__{token}.owner" marker; cleanup
# helpers probe the PID before removing running containers so we never kill a
# container belonging to a concurrent script instance (e.g. a different agent/model
# run for the same task that happens to share the same harness image).
def _default_run_owner_dir() -> Path:
    user = (os.getenv("USER") or getpass.getuser() or "unknown").strip()
    user = re.sub(r"[^A-Za-z0-9_.-]+", "_", user) or "unknown"
    return Path(f"/tmp/hil_bench_run_owners_{user}")


RUN_OWNER_DIR = Path(os.getenv("HIL_BENCH_RUN_OWNER_DIR") or str(_default_run_owner_dir()))

# ── Attempt-start stagger (mirrors paper_pipeline.py: 20 s between launches) ─
# Ensures the LiteLLM proxy / model API is not hammered with simultaneous cold
# starts.  The lock + timestamp pattern mirrors paper_pipeline._wait_for_launch_slot.
ATTEMPT_START_STAGGER_SECONDS = 20
_stagger_lock = threading.Lock()
_next_start_time: float = 0.0  # monotonic clock time after which the next attempt may start


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

# SDK-specific configuration.  Values are overridden in main() based on --sdk.
SDK_CONFIGS = {
    "claude": {
        "harness_image_prefix": "hilbench-swe-harness-claude",
        "entrypoint":           "/opt/trust_horizon/src/hil_swe/run_claude.mjs",
        "model_env_key":        "CLAUDE_MODEL",
        "default_model":        "claude-opus-4-7",
        "executable_env":       "CLAUDE_CODE_EXECUTABLE=claude",
        # runtime defaults to "node" when absent
    },
    "codex": {
        "harness_image_prefix": "hilbench-swe-harness-codex",
        "entrypoint":           "/opt/trust_horizon/src/hil_swe/run_codex.mjs",
        "model_env_key":        "CODEX_MODEL",
        "default_model":        "gpt-5.5",
        "executable_env":       "CODEX_CODE_EXECUTABLE=codex",
    },
    "adk": {
        "harness_image_prefix": "hilbench-swe-harness-adk",
        "entrypoint":           "/opt/trust_horizon/src/hil_swe/run_adk.py",
        "model_env_key":        "ADK_MODEL",
        "default_model":        "gemini/gemini-3.1-pro-preview-customtools",
        "executable_env":       "ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS=true",
        # python3.adk is a versioned symlink created by Dockerfile.harness that
        # points to an isolated ADK virtualenv (Python >=3.10) with
        # google-adk/litellm/skills dependencies installed. It deliberately
        # avoids overriding the task's own python3
        # (which may be 3.8) so agent bash tool calls like "python3 -m pytest"
        # still use the task's expected Python and its installed packages.
        "runtime":              "python3.adk",
    },
    "opencode": {
        "harness_image_prefix": "hilbench-swe-harness-opencode",
        "entrypoint":           "/opt/trust_horizon/src/hil_swe/run_opencode.mjs",
        "model_env_key":        "OPENCODE_MODEL",
        "default_model":        "fireworks_ai/glm-5p1",
        # Suppress auto-update banner; belt-and-suspenders with autoupdate:false in config
        "executable_env":       "OPENCODE_NO_UPDATE=1",
        # runtime defaults to "node" when absent
    },
}
DEFAULT_SDK = "claude"

# Module-level vars — set to SDK-specific values in main() before any workers start.
SDK                  = DEFAULT_SDK
HARNESS_IMAGE_PREFIX = SDK_CONFIGS[DEFAULT_SDK]["harness_image_prefix"]
ENTRYPOINT           = SDK_CONFIGS[DEFAULT_SDK]["entrypoint"]

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
    "CLAUDE_REASONING_EFFORT",
    "CLAUDE_EFFORT",  # backward-compat alias
    "CODEX_MODEL",
    "CODEX_REASONING_EFFORT",
    "ADK_MODEL",
    "ADK_REASONING_EFFORT",
    "OPENCODE_MODEL",
    "OPENCODE_REASONING_EFFORT",
    "OPENCODE_REASONING",  # backward-compat alias
    "OPENCODE_STARTUP_TIMEOUT_MS",
    "LITELLM_CALL_TIMEOUT_MS",
    "STEP_LITELLM_TRIES",
    "WITH_CUSTOM_TOOL",
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

MODEL_REASONING_DEFAULTS = [
    # Claude Code models
    ("claude-opus-4-7", {"CLAUDE_REASONING_EFFORT": "max"}),
    # Codex models
    ("gpt-5.5", {"CODEX_REASONING_EFFORT": "xhigh"}),
    # ADK model (routed through LiteLLM)
    ("gemini/gemini-3.1-pro-preview-customtools", {"ADK_REASONING_EFFORT": "high"}),
    # OpenCode model
    ("fireworks_ai/glm-5p1", {"OPENCODE_REASONING_EFFORT": "xhigh"}),
]


def default_reasoning_env_for_model(model: str) -> dict[str, str]:
    """Return default reasoning env override for exact model ids."""
    m = (model or "").strip()
    if not m:
        return {}
    lower = m.lower()

    # Exact matches only
    for model_id, envs in MODEL_REASONING_DEFAULTS:
        if lower == model_id.lower():
            return dict(envs)
    return {}


def reasoning_env_for_sdk(sdk: str, effort: str) -> dict[str, str]:
    """Map a generic effort level to SDK-specific env vars."""
    eff = effort.strip().lower()
    if sdk == "claude":
        return {"CLAUDE_REASONING_EFFORT": eff}
    if sdk == "codex":
        # Codex supports up to xhigh (no "max" literal).
        return {"CODEX_REASONING_EFFORT": "xhigh" if eff == "max" else eff}
    if sdk == "adk":
        # Forward as a provider hint; run_adk.py applies this best-effort via LiteLLM kwargs.
        return {"ADK_REASONING_EFFORT": eff}
    if sdk == "opencode":
        return {"OPENCODE_REASONING_EFFORT": eff}
    return {}

_print_lock = threading.Lock()


# ── Per-uid owner tokens (mirrors paper_pipeline.py _register_attempt_owner) ─
#
# Token filename: "{uid}__{pid}__{uuid}.owner"
# Each run_attempt() call registers a token for its uid at entry and unregisters
# it in a try/finally, so tokens always reflect live passes.  Cleanup queries
# only the specific uid's tokens, so a concurrent script instance running a
# *different* uid cannot block cleanup of an orphaned container here.

def _register_uid_owner(uid: str) -> Path:
    """Write a per-uid PID token; returns the token path for later unregistration."""
    RUN_OWNER_DIR.mkdir(parents=True, exist_ok=True)
    token = RUN_OWNER_DIR / f"{uid}__{os.getpid()}__{uuid.uuid4().hex}.owner"
    token.write_text(str(os.getpid()))
    return token


def _unregister_uid_owner(token: Path | None) -> None:
    if not token:
        return
    token.unlink(missing_ok=True)


def _uid_has_live_owner(uid: str) -> bool:
    """True when any registered owner process for this uid is still alive.

    Mirrors paper_pipeline._attempt_has_live_owner:  probe the PID with kill(0),
    delete stale tokens for dead processes, be conservative (return True) on
    PermissionError (different user / same uid).
    """
    if not RUN_OWNER_DIR.exists():
        return False
    for marker in RUN_OWNER_DIR.glob(f"{uid}__*__*.owner"):
        parts = marker.name.split("__")
        if len(parts) < 3:
            marker.unlink(missing_ok=True)
            continue
        try:
            pid = int(parts[1])
        except Exception:
            marker.unlink(missing_ok=True)
            continue
        try:
            os.kill(pid, 0)
            return True          # process is alive → uid still has a live owner
        except ProcessLookupError:
            marker.unlink(missing_ok=True)   # stale — clean up
        except PermissionError:
            return True          # different user; be conservative
    return False


def cleanup_orphaned_containers(harness_image: str, uid: str) -> int:
    """Remove containers for one uid that exited or are running with no live owner.

    Mirrors paper_pipeline.cleanup_swe_containers_for_attempt exactly:
    - Two docker queries: by ancestor image AND by container name prefix.
      The name filter catches containers whose ancestor tracking is stale
      (e.g. image rebuilt with the same tag after the container started).
    - Exited containers: always remove (they're done).
    - Running containers: only remove if _uid_has_live_owner(uid) is False.
      This is uid-scoped, so a concurrent script owning a *different* uid
      cannot block cleanup here.
    """
    # 5-field format mirrors paper_pipeline: ID, Image, Names, Status, RunningFor.
    # Status is at index 3.
    _FMT = "{{.ID}}\t{{.Image}}\t{{.Names}}\t{{.Status}}\t{{.RunningFor}}"
    # Container name prefix for this uid (all passes/modes/runs share this prefix).
    container_name_prefix = f"th-swe-{uid[:12]}-"
    try:
        by_ancestor = subprocess.run(
            ["docker", "ps", "-a", "--format", _FMT,
             "--filter", f"ancestor={harness_image}"],
            capture_output=True, text=True, check=False,
        )
        by_name = subprocess.run(
            ["docker", "ps", "-a", "--format", _FMT,
             "--filter", f"name={container_name_prefix}"],
            capture_output=True, text=True, check=False,
        )
        to_remove: set[str] = set()
        for source in (by_ancestor.stdout, by_name.stdout):
            for line in source.splitlines():
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                cid, status = parts[0], parts[3].lower()
                if status.startswith("exited"):
                    to_remove.add(cid)
                elif status.startswith("up"):
                    if not _uid_has_live_owner(uid):
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
    ts = datetime.now().strftime("%H:%M:%S")
    with _print_lock:
        print(f"[{ts}] {msg}", file=file, flush=True)


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


def docker_image_exists(image_name: str) -> bool:
    r = subprocess.run(["docker", "image", "inspect", image_name], capture_output=True, check=False)
    return r.returncode == 0


def output_dir_for(run_id: str, uid: str, mode: str, pass_index: int) -> Path:
    return RUNS_DIR / run_id / uid / mode / f"pass_{pass_index}"


def _run_id_token(run_id: str) -> str:
    """Stable short token for container names, collision-resistant across run_ids."""
    return hashlib.sha1(str(run_id).encode("utf-8")).hexdigest()[:12]


SYSTEM_ERROR_STOP_REASONS = {
    "sdk_error",
    "timeout",
    "sidecar_start_failed",
    "proxy_start_failed",
}


def _load_result_json(out_dir: Path) -> dict | None:
    result_path = out_dir / "result.json"
    if not result_path.exists():
        return None
    try:
        data = json.loads(result_path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _result_has_system_error(result: dict) -> bool:
    sdk_error = str(result.get("sdk_error") or "").strip()
    if sdk_error:
        return True
    stop_reason = str(result.get("stop_reason") or "").strip().lower()
    return stop_reason in SYSTEM_ERROR_STOP_REASONS


def result_is_complete(out_dir: Path) -> bool:
    result = _load_result_json(out_dir)
    if result is None:
        return False
    # Any harness/system error means this pass must be rerun.
    return not _result_has_system_error(result)


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

    # Register a per-uid owner token so cleanup_orphaned_containers knows this
    # pass is active.  Unregistered in a finally so it always fires, even on
    # timeout or exception.  Mirrors paper_pipeline._register_attempt_owner.
    # Initialization to None + inner try/except mirrors paper_pipeline exactly:
    # registration failure is a non-fatal warning, not a hard error.
    owner_token: Path | None = None
    try:
        owner_token = _register_uid_owner(uid)
    except Exception as exc:
        log(f"[{uid[:12]}|{mode}|p{pass_index}] WARNING: failed to register owner token: {exc}",
            file=sys.stderr)
    try:
        return _run_attempt_inner(
            uid=uid,
            harness_image=harness_image,
            mode=mode,
            pass_index=pass_index,
            run_id=run_id,
            out_dir=out_dir,
            task_dir=task_dir,
            extra_env=extra_env,
        )
    finally:
        _unregister_uid_owner(owner_token)


def _run_attempt_inner(
    *,
    uid: str,
    harness_image: str,
    mode: str,
    pass_index: int,
    run_id: str,
    out_dir: Path,
    task_dir: Path,
    extra_env: dict[str, str],
) -> tuple[bool, str]:
    """Build + run the docker container for one (uid, mode, pass_index) pass."""
    # Remove stale trajectory.jsonl from any previous run format that wrote it.
    stale_jsonl = out_dir / "trajectory.jsonl"
    if stale_jsonl.exists():
        stale_jsonl.unlink()

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
        "-e", SDK_CONFIGS[SDK]["executable_env"],
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

    # Unique container name for targeted cleanup on timeout.
    # Format: th-swe-<uid12>-<mode>-p<pass>-r<run_id_hash12>
    container_name = f"th-swe-{uid[:12]}-{mode}-p{pass_index}-r{_run_id_token(run_id)}"

    cmd = [
        "docker", "run",
        "--rm",                         # auto-remove on clean exit
        "--name", container_name,       # named for targeted kill on timeout
        # Allow container to reach host services (LiteLLM proxy, vLLM judge server).
        # --add-host maps host.docker.internal → host gateway (same pattern as hil-bench
        # configs/swe/ask_config_claude_opus_4-6.yaml).  Clients should use
        # http://host.docker.internal:PORT rather than http://localhost:PORT.
        "--add-host=host.docker.internal:host-gateway",
        *_resolve_litellm_add_host_args(extra_env),
        # task data (read-only)
        "-v", f"{task_dir.resolve()}:/task:ro",
        # harness source (read-only) — changes don't need image rebuilds
        "-v", f"{SRC_DIR.resolve()}:/opt/trust_horizon/src:ro",
        # output (read-write)
        "-v", f"{out_dir.resolve()}:/output",
        *env_args,
        harness_image,
        SDK_CONFIGS[SDK].get("runtime", "node"), ENTRYPOINT,
    ]

    label = f"[{uid[:12]}|{mode}|p{pass_index}]"

    # ── Stagger: wait until our turn, then advance the global launch clock ────
    # Mirrors paper_pipeline.py _wait_for_launch_slot (20 s between attempt starts).
    # The stagger happens right before the container launches, after all pre-checks,
    # so validation/setup is not delayed.
    global _next_start_time
    with _stagger_lock:
        now = time.monotonic()
        delay = max(0.0, _next_start_time - now)
        if delay > 0:
            time.sleep(delay)
        _next_start_time = max(time.monotonic(), _next_start_time) + ATTEMPT_START_STAGGER_SECONDS

    log(f"{label} Starting container {harness_image}")

    container_log = out_dir / "container.log"
    started_at = time.time()
    host_timeout = int(os.environ.get("ATTEMPT_TIMEOUT_MS", "10800000")) // 1000 + 120
    proc: subprocess.Popen | None = None
    try:
        with open(container_log, "w") as log_fh:
            # Use Popen (not run) so we can explicitly kill the docker CLI process on
            # timeout.  subprocess.run(timeout=...) raises TimeoutExpired but does NOT
            # kill the child — it would leave an orphaned `docker run` process.
            proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT)
            try:
                proc.wait(timeout=host_timeout)
            except subprocess.TimeoutExpired:
                # Kill the docker CLI process first, then the container.
                proc.kill()
                proc.wait()  # reap so no zombie
                elapsed = int(time.time() - started_at)
                msg = f"{label} Timed out on host after {elapsed}s — killing container {container_name}"
                log(msg, file=sys.stderr)
                subprocess.run(["docker", "rm", "-f", container_name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                return False, msg
        elapsed = int(time.time() - started_at)
        if proc.returncode == 0:
            log(f"{label} Done in {elapsed}s ✓")
            return True, f"{label} done in {elapsed}s"
        else:
            tail = _tail(container_log, 20)
            msg = f"{label} Container exited {proc.returncode} after {elapsed}s. Last lines:\n{tail}"
            log(msg, file=sys.stderr)
            return False, msg
    except Exception as exc:
        msg = f"{label} Exception: {exc}"
        log(msg, file=sys.stderr)
        # Kill the docker CLI process if it's still running.
        if proc is not None:
            try:
                proc.kill()
                proc.wait()
            except Exception:
                pass
        subprocess.run(["docker", "rm", "-f", container_name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return False, msg


def _tail(path: Path, n: int) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def _resolve_litellm_add_host_args(extra_env: dict[str, str]) -> list[str]:
    """Return docker --add-host args for LITELLM_BASE_URL when needed."""
    base_url = (extra_env.get("LITELLM_BASE_URL") or os.environ.get("LITELLM_BASE_URL") or "").strip()
    if not base_url:
        return []
    try:
        host = (urlparse(base_url).hostname or "").strip()
        if not host or host in {"localhost", "127.0.0.1", "host.docker.internal"}:
            return []
        _, _, ips = socket.gethostbyname_ex(host)
        if not ips:
            return []
        return [f"--add-host={host}:{ips[0]}"]
    except Exception:
        return []


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
    uid_group.add_argument("--uids", nargs="+", metavar="UID", help="Specific attempt UIDs to run.")
    uid_group.add_argument(
        "--p-set", choices=["public", "private", "both"],
        help=(
            "Run all ingested tasks for the given partition: "
            "'public' = 100 public tasks, 'private' = 50 private tasks, "
            "'both' = all 150 tasks."
        ),
    )
    parser.add_argument(
        "--sdk", choices=list(SDK_CONFIGS), default=DEFAULT_SDK,
        help=f"Agent SDK to use (default: {DEFAULT_SDK}). "
             f"Determines the harness image prefix and entrypoint. "
             f"Supported: {', '.join(SDK_CONFIGS)}.",
    )
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
        help="Max concurrent Docker containers. Defaults to min(num_jobs, 10).",
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
        "--eval-timeout", type=int, default=3600,
        help="Per-attempt eval timeout in seconds (default: 3600).",
    )
    parser.add_argument(
        "--max-turns", type=int, default=None,
        help="Max agent turns per attempt (default: 200). "
             "Equivalent to passing --env MAX_TURNS=N.",
    )
    parser.add_argument(
        "--max-reasoning",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Enable highest-supported reasoning defaults for the selected SDK "
            "(default: enabled; use --no-max-reasoning to disable)."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=None,
        help=(
            "Override reasoning effort tier for the selected SDK. "
            "Takes precedence over --max-reasoning unless the SDK-specific env var "
            "is explicitly set via --env."
        ),
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
    parser.add_argument(
        "--with-custom-tool",
        action="store_true",
        default=False,
        help=(
            "Enable an additional top-level custom ask_human tool for claude/codex SDK "
            "runs only. Does not replace native question-asking tools."
        ),
    )
    args = parser.parse_args()

    # ── Apply SDK-specific globals before any worker threads are started ─────────
    global SDK, HARNESS_IMAGE_PREFIX, ENTRYPOINT
    SDK                  = args.sdk
    HARNESS_IMAGE_PREFIX = SDK_CONFIGS[args.sdk]["harness_image_prefix"]
    ENTRYPOINT           = SDK_CONFIGS[args.sdk]["entrypoint"]
    log(f"SDK: {args.sdk}  harness: {HARNESS_IMAGE_PREFIX}  entrypoint: {Path(ENTRYPOINT).name}")

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
    explicit_env_override_keys: set[str] = set()
    for item in args.env or []:
        if "=" in item:
            k, v = item.split("=", 1)
            effective_env[k] = v
            explicit_env_override_keys.add(k)

    # 4. --max-turns shorthand (equivalent to --env MAX_TURNS=N)
    if args.max_turns is not None:
        effective_env["MAX_TURNS"] = str(args.max_turns)

    # 4a. Ensure selected SDK always has an explicit model env value.
    # This guarantees container-side runners use the same default model policy
    # even when .env omits the model key and no --env override is provided.
    sdk_cfg_for_model = SDK_CONFIGS[args.sdk]
    model_env_key = sdk_cfg_for_model["model_env_key"]
    if not effective_env.get(model_env_key):
        effective_env[model_env_key] = sdk_cfg_for_model["default_model"]

    # 4b. Optional custom ask_human tool exposure (claude/codex only)
    if args.with_custom_tool:
        if args.sdk not in {"claude", "codex"}:
            print(
                "ERROR: --with-custom-tool is only supported with --sdk claude or --sdk codex.",
                file=sys.stderr,
            )
            sys.exit(1)
        effective_env["WITH_CUSTOM_TOOL"] = "1"

    # 5. Reasoning defaults/overrides (unless SDK-specific key explicitly set via --env)
    sdk_cfg_for_reasoning = SDK_CONFIGS[args.sdk]
    model_for_reasoning = effective_env.get(
        sdk_cfg_for_reasoning["model_env_key"],
        sdk_cfg_for_reasoning["default_model"],
    )
    if args.reasoning_effort is not None:
        requested = reasoning_env_for_sdk(args.sdk, args.reasoning_effort)
        for k, v in requested.items():
            if k not in explicit_env_override_keys:
                effective_env[k] = v
    elif args.max_reasoning:
        for k, v in default_reasoning_env_for_model(model_for_reasoning).items():
            if k not in explicit_env_override_keys and not effective_env.get(k):
                effective_env[k] = v

    # Backward-compat aliases for previously used names.
    if "CLAUDE_REASONING_EFFORT" not in effective_env and effective_env.get("CLAUDE_EFFORT"):
        effective_env["CLAUDE_REASONING_EFFORT"] = effective_env["CLAUDE_EFFORT"]
    if "OPENCODE_REASONING_EFFORT" not in effective_env and effective_env.get("OPENCODE_REASONING"):
        old = str(effective_env["OPENCODE_REASONING"]).strip().lower()
        effective_env["OPENCODE_REASONING_EFFORT"] = "max" if old in {"1", "true", "yes", "on"} else "low"

    # 6. Validate the minimum required vars
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
    sdk_cfg = SDK_CONFIGS[args.sdk]
    model = effective_env.get(sdk_cfg["model_env_key"], sdk_cfg["default_model"])
    log(f"Proxy: {base_url}  Model: {model}")
    reasoning_cfg = {
        "claude": effective_env.get("CLAUDE_REASONING_EFFORT", "(default)"),
        "codex": effective_env.get("CODEX_REASONING_EFFORT", "(default)"),
        "adk": effective_env.get("ADK_REASONING_EFFORT", "(default)"),
        "opencode": effective_env.get("OPENCODE_REASONING_EFFORT", "(default)"),
    }
    log(f"Reasoning config [{args.sdk}]: {reasoning_cfg.get(args.sdk, '(default)')}")

    tasks = load_tasks_index()
    by_uid = {t["uid"]: t for t in tasks}

    if args.p_set:
        target_tasks = filter_tasks_by_pset(tasks, args.p_set)
        log(f"p-set: {args.p_set}  →  {len(target_tasks)} task(s) selected from {len(tasks)} ingested")
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

    run_dir = RUNS_DIR / args.run_id
    pending_pass_keys: set[tuple[str, str, int]] = set()
    for key in all_pass_keys:
        uid, mode, pass_idx = key
        pass_dir = run_dir / uid / mode / f"pass_{pass_idx}"
        solve_complete = result_is_complete(pass_dir)
        has_eval = (pass_dir / "eval_result.json").exists()
        if args.force:
            pending_pass_keys.add(key)
        elif args.skip_eval:
            if not solve_complete:
                pending_pass_keys.add(key)
        else:
            # For normal solve+eval runs, a pass is considered complete only when
            # both solve and eval artifacts exist.
            if not (solve_complete and has_eval):
                pending_pass_keys.add(key)

    solve_jobs = build_job_list(
        target_tasks, args.modes, args.passes, args.run_id, skip_if_complete=not args.force
    )
    total = len(pending_pass_keys)
    skipped_solve = len(all_pass_keys) - len(solve_jobs)
    completed_runs = 0
    completed_runs_lock = threading.Lock()
    # Protect against 0-worker executor when there are no solve jobs
    workers = max(1, args.workers if args.workers is not None else min(len(solve_jobs) or 1, 10))
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

    def eval_one(job: dict, force_eval: bool = False) -> tuple[bool, str]:
        # force_eval=True when the solve just ran in this invocation: a new patch
        # was produced, so any stale eval_result.json from a previous broken run
        # must not suppress re-evaluation.  Without this, a task whose first solve
        # failed (wrote a bad patch + eval_result.json), then had its image rebuilt
        # and re-solved correctly, would silently keep the stale failing eval result.
        ok, msg = _eval_attempt(
            uid=job["uid"],
            mode=job["mode"],
            pass_index=job["pass_index"],
            run_id=args.run_id,
            skip_if_complete=not args.force and not force_eval,
            timeout_s=args.eval_timeout,
        )
        nonlocal completed_runs
        with completed_runs_lock:
            completed_runs += 1
            done = completed_runs
        # Log immediately from the eval thread so the message appears as soon as
        # the eval finishes, not after all solve futures have been drained.
        log(f"  Eval  {'✓' if ok else '✗'} {msg} [{done}/{total} done]")
        return ok, msg

    # ── Pipelined Solve → Eval (concurrent) ───────────────────────────────────
    # Each pass is fully independent.  As soon as a solve container exits, its
    # eval container is queued immediately — we don't wait for other passes.
    # Both thread pools run concurrently, bounded by their respective worker limits.
    # Phase 3 (metrics) runs after all evals finish.

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
                if not ok:
                    with completed_runs_lock:
                        completed_runs += 1
                log(f"  Solve {'✓' if ok else '✗'} {msg}")

                if eval_exec is not None and ok:
                    key = (j["uid"], j["mode"], j["pass_index"])
                    if key not in submitted_eval_keys:
                        # build_job_list already excluded passes with result.json, so
                        # any job that returned ok=True here actually ran a new solve.
                        # Always force re-eval so a stale eval_result.json from a
                        # previous broken run cannot hide the new result.
                        ef = eval_exec.submit(eval_one, j, True)
                        eval_futures[ef] = j
                        submitted_eval_keys.add(key)

            # Also submit evals for any already-solved passes from a previous run
            # that weren't just solved now (e.g. --force was not set and they had
            # result.json already).  These passes were NOT re-solved in this run so
            # we respect skip_if_complete (force_eval=False) — if their eval is
            # already good, no reason to re-run it.
            if eval_exec is not None:
                for uid, mode, pass_idx in sorted(pending_pass_keys - submitted_eval_keys):
                    pass_dir = run_dir / uid / mode / f"pass_{pass_idx}"
                    # Only queue eval for passes with a complete solve result.
                    # Timeout solves write result.json with sdk_error and no patch;
                    # those must be rerun in solve, not fed to eval.
                    if result_is_complete(pass_dir):
                        eval_job = {"uid": uid, "mode": mode, "pass_index": pass_idx}
                        ef = eval_exec.submit(eval_one, eval_job, False)
                        eval_futures[ef] = eval_job
                        submitted_eval_keys.add((uid, mode, pass_idx))

            # Collect eval results (logging already done inside eval_one).
            for ef in as_completed(eval_futures):
                ok2, msg2 = ef.result()
                (eval_ok if ok2 else eval_fail).append(msg2)

    finally:
        # Per-uid tokens are unregistered inside run_attempt / eval_attempt's own finally.
        # This end-of-run pass is a last-resort safety net for any containers
        # that slipped through (e.g. SIGKILL during a run).
        cleaned_solve = 0
        cleaned_eval = 0
        for task in target_tasks:
            harness_image = f"{HARNESS_IMAGE_PREFIX}:{task['uid']}"
            cleaned_solve += cleanup_orphaned_containers(harness_image, task["uid"])
            if not args.skip_eval:
                cleaned_eval += cleanup_orphaned_eval_containers(task["uid"])
        if cleaned_solve > 0:
            log(f"Cleaned up {cleaned_solve} orphaned harness container(s)")
        if cleaned_eval > 0:
            log(f"Cleaned up {cleaned_eval} orphaned eval container(s)")

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
            # Ensure metrics cover the full requested run grid (all targeted
            # uid/mode/pass combinations), not just the subset that happened to
            # produce files during this invocation.
            row_keys = {
                (str(r.get("uid")), str(r.get("mode")), int(r.get("pass_index", -1)))
                for r in rows
                if r.get("uid") is not None and r.get("mode") is not None and r.get("pass_index") is not None
            }
            for uid, mode, pass_idx in sorted(all_pass_keys):
                key = (uid, mode, pass_idx)
                if key in row_keys:
                    continue
                pass_dir = run_dir / uid / mode / f"pass_{pass_idx}"
                rows.append({
                    "uid": uid,
                    "mode": mode,
                    "agent": args.sdk,
                    "model": model,
                    "pass_index": pass_idx,
                    # Treat missing rows as unresolved so summary coverage is
                    # over the full run scope rather than only completed rows.
                    "status": "unresolved",
                    "resolved": False,
                    "num_steps": 0,
                    "num_questions": 0,
                    "num_questions_approval": 0,
                    "num_total_questions": 0,
                    "num_questions_full_info": 0,
                    "num_blockers_resolved": 0,
                    "num_blockers_total": 0,
                    "patch_bytes": None,
                    "pass_dir": str(pass_dir),
                })

            rows.sort(key=lambda r: (str(r.get("uid", "")), str(r.get("mode", "")), int(r.get("pass_index", 0))))
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
