"""
hilbench setup — pre-flight checker for HiL-Dynamics / HiL-Bench runs.

Verifies that all required tools, credentials, data, and Docker images are in
place before launching a benchmark run.

Usage:
  python3 scripts/hilbench_setup.py
  python3 scripts/hilbench_setup.py --strict
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Allow importing siblings (load_dotenv, find_env_file, ROOT, etc.)
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from run_hil_swe import load_dotenv, find_env_file, ROOT, TASKS_INDEX  # noqa: E402

# ── ANSI colours ──────────────────────────────────────────────────────────────
_GREEN = "\033[32m"
_RED   = "\033[31m"
_RESET = "\033[0m"
_BOLD  = "\033[1m"


def _ok(msg: str) -> None:
    print(f"  {_GREEN}✓{_RESET} {msg}")


def _fail(msg: str, fix: str | None = None) -> None:
    print(f"  {_RED}✗{_RESET} {msg}")
    if fix:
        print(f"    → {fix}")


# ── Checks ────────────────────────────────────────────────────────────────────

def check_python() -> bool:
    v = sys.version_info
    if v >= (3, 10):
        _ok(f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    _fail(f"Python {v.major}.{v.minor}.{v.micro} — need ≥ 3.10",
          fix="Upgrade to Python 3.10+")
    return False


def check_node() -> bool:
    node = shutil.which("node")
    if not node:
        _fail("Node.js not found", fix="Install Node.js 20+: https://nodejs.org")
        return False
    result = subprocess.run(["node", "--version"], capture_output=True, text=True)
    ver = result.stdout.strip()
    _ok(f"Node.js {ver}")
    return True


def check_docker() -> bool:
    if not shutil.which("docker"):
        _fail("docker not found", fix="Install Docker: https://docs.docker.com/get-docker/")
        return False
    result = subprocess.run(["docker", "info"], capture_output=True)
    if result.returncode != 0:
        _fail("Docker daemon not running", fix="Start Docker Desktop or run: sudo systemctl start docker")
        return False
    _ok("Docker running")
    return True


def check_env() -> tuple[bool, dict[str, str], Path | None]:
    env_path = find_env_file()
    env: dict[str, str] = {}
    if env_path:
        env = load_dotenv(env_path)
        _ok(f"credential env found at {env_path}")
    else:
        _ok("no credential env file found; checking process environment")

    # Process env wins over files, matching the run harness behavior.
    for key in (
        "LITELLM_BASE_URL",
        "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL",
        "LITELLM_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "LITELLM_PROXY_API_KEY",
        "OPENAI_API_KEY",
        "LITELLM_AWS_SECRET_ID",
        "AWS_SECRET_ID",
        "LITELLM_AWS_SECRET_KEY",
        "AWS_SECRET_KEY_NAME",
        "AWS_PROFILE",
        "AWS_REGION",
    ):
        if os.environ.get(key):
            env[key] = os.environ[key]

    has_base_url = bool(env.get("LITELLM_BASE_URL") or env.get("ANTHROPIC_BASE_URL") or env.get("OPENAI_BASE_URL"))
    has_direct_key = bool(env.get("LITELLM_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN") or env.get("LITELLM_PROXY_API_KEY") or env.get("OPENAI_API_KEY"))
    has_aws_key = bool(
        (env.get("LITELLM_AWS_SECRET_ID") or env.get("AWS_SECRET_ID"))
        and (env.get("LITELLM_AWS_SECRET_KEY") or env.get("AWS_SECRET_KEY_NAME"))
    )

    if not has_base_url:
        _fail("LiteLLM base URL missing",
              fix="Set LITELLM_BASE_URL in .env, LITELLM_CREDENTIALS_FILE, or the process environment")
        return False, env, env_path
    if not (has_direct_key or has_aws_key):
        _fail("No API key source found",
              fix="Set LITELLM_API_KEY/ANTHROPIC_AUTH_TOKEN or AWS secret env vars")
        return False, env, env_path

    _ok("LITELLM credentials present")
    return True, env, env_path


def check_ask_human_judge(env: dict[str, str], env_path: Path | None) -> bool:
    run_env = os.environ.copy()
    run_env.update(env)
    if env_path:
        run_env.setdefault("LITELLM_CREDENTIALS_FILE", str(env_path))
    if not run_env.get("ASK_HUMAN_MODEL"):
        raise RuntimeError("ASK_HUMAN_MODEL is not set. Configure it in .env (see .env.example).")
    cmd = ["node", "tests/judge_calibration/run.mjs", "--quick"]
    result = subprocess.run(cmd, cwd=ROOT, env=run_env, capture_output=True, text=True, timeout=180)
    if result.returncode == 0:
        _ok(f"ask_human judge probe ({run_env['ASK_HUMAN_MODEL']})")
        return True
    detail = (result.stderr or result.stdout or "").strip()
    _fail(
        f"ask_human judge probe failed for {run_env['ASK_HUMAN_MODEL']}",
        fix=detail[-1000:] if detail else "Verify LITELLM credentials and ASK_HUMAN_MODEL.",
    )
    return False


def check_tasks_index() -> bool:
    if TASKS_INDEX.exists():
        try:
            import json
            index = json.loads(TASKS_INDEX.read_text())
            n = len(index) if isinstance(index, (list, dict)) else "?"
            _ok(f"tasks_index.json found ({n} tasks)")
        except Exception:
            _ok(f"tasks_index.json found")
        return True
    _fail(
        f"tasks_index.json not found at {TASKS_INDEX}",
        fix="python3 scripts/ingest_hil_swe.py --p-set public",
    )
    return False


def check_runs_dir() -> bool:
    runs = ROOT / "runs"
    try:
        runs.mkdir(exist_ok=True)
        test = runs / ".hilbench_write_test"
        test.touch()
        test.unlink()
        _ok("runs/ directory writable")
        return True
    except OSError as e:
        _fail(f"runs/ not writable: {e}", fix=f"mkdir -p {runs} && chmod u+w {runs}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-flight checker for HiL-Bench runs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also run live ask_human judge calibration probe.",
    )
    args = parser.parse_args()

    print(f"\n{_BOLD}HiL-Dynamics — setup check{_RESET}\n")

    results: list[bool] = []

    results.append(check_python())
    results.append(check_node())
    results.append(check_docker())
    env_ok, env, env_path = check_env()
    results.append(env_ok)
    if args.strict and env_ok:
        results.append(check_ask_human_judge(env, env_path))
    results.append(check_tasks_index())
    results.append(check_runs_dir())

    print()
    if all(results):
        print(f"{_GREEN}{_BOLD}All checks passed. Ready to run.{_RESET}\n")
        return 0
    else:
        failed = sum(1 for r in results if not r)
        print(f"{_RED}{_BOLD}{failed} check(s) failed. Fix the issues above before running.{_RESET}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
