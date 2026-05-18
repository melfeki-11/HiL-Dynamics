"""
hilbench run — launch a HiL-Bench benchmark run from YAML config files.

Reads a harness config and a slice config, translates them into run_hil_swe.py
arguments, and calls the orchestrator via subprocess (keeping logging isolation).

Usage:
  python3 scripts/hilbench_run.py --harness claude --slice smoke
  python3 scripts/hilbench_run.py --harness claude --slice test20 --dry-run
  python3 scripts/hilbench_run.py \\
      --harness configs/harnesses/claude.yaml \\
      --slice configs/slices/smoke.yaml \\
      --run-id my-first-run

The --harness and --slice arguments accept either:
  - a short name (e.g. "claude", "smoke") resolved relative to configs/
  - a path to a .yaml file (absolute or relative to repo root)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml  # type: ignore[import]
except ImportError:
    print("hilbench run requires pyyaml.  Install it with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

_SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = _SCRIPTS_DIR.parent

# Maps sdk name → env var key used by run_hil_swe.py for the model override
_SDK_MODEL_ENV: dict[str, str] = {
    "claude":   "CLAUDE_MODEL",
    "codex":    "CODEX_MODEL",
    "adk":      "ADK_MODEL",
    "opencode": "OPENCODE_MODEL",
}


def _resolve_config(name: str, kind: str) -> Path:
    """Resolve a config name or path to an absolute .yaml path."""
    p = Path(name)
    if p.suffix == ".yaml":
        return p if p.is_absolute() else ROOT / p
    # Short name: look in configs/<kind>/
    candidate = ROOT / "configs" / kind / f"{name}.yaml"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f"Config '{name}' not found. "
        f"Tried: {candidate}\n"
        f"Available {kind}: "
        + ", ".join(p.stem for p in (ROOT / "configs" / kind).glob("*.yaml"))
    )


def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}, got {type(data)}")
    return data


def _load_uids_file(path_value: str) -> list[str]:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    uids: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        uids.append(line)
    if not uids:
        raise ValueError(f"No UIDs found in {path}")
    return uids


def _auto_run_id(sdk: str, slice_name: str) -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = Path(slice_name).stem  # handles both "smoke" and "configs/slices/smoke.yaml"
    return f"{sdk}_{stem}_{ts}"


def build_argv(
    harness: dict,
    slice_cfg: dict,
    run_id: str,
    *,
    allow_test_set: bool = False,
    model_override: str | None = None,
    arm_override: str | None = None,
) -> list[str]:
    """Translate harness + slice YAML dicts into run_hil_swe.py argv."""
    sdk = harness.get("sdk", "claude")
    argv: list[str] = [
        sys.executable,
        str(_SCRIPTS_DIR / "run_hil_swe.py"),
        "--run-id", run_id,
        "--sdk", sdk,
    ]

    # Model override (passed as --env KEY=VALUE)
    model = model_override or harness.get("model")
    if model:
        model_env_key = _SDK_MODEL_ENV.get(sdk, f"{sdk.upper()}_MODEL")
        argv += ["--env", f"{model_env_key}={model}"]

    # Reasoning effort
    effort = harness.get("reasoning_effort")
    if effort:
        argv += ["--reasoning-effort", str(effort)]

    # UIDs or p_set (mutually exclusive in run_hil_swe.py)
    uids = slice_cfg.get("uids")
    uids_file = slice_cfg.get("uids_file")
    p_set = slice_cfg.get("p_set")
    if uids_file:
        argv += ["--uids"] + _load_uids_file(str(uids_file))
    elif uids:
        argv += ["--uids"] + [str(u) for u in uids]
    elif p_set:
        argv += ["--p-set", str(p_set)]
    else:
        raise ValueError("Slice config must specify either 'uids' or 'p_set'")

    # Modes
    modes = [arm_override] if arm_override else slice_cfg.get("modes", ["neutral"])
    argv += ["--modes"] + [str(m) for m in modes]

    # Passes
    passes = slice_cfg.get("passes")
    if passes is not None:
        argv += ["--passes", str(passes)]

    # Workers
    workers = slice_cfg.get("workers")
    if workers is not None:
        argv += ["--workers", str(workers)]

    # Max turns
    max_turns = slice_cfg.get("max_turns")
    if max_turns is not None:
        argv += ["--max-turns", str(max_turns)]

    if slice_cfg.get("held_out") and not allow_test_set:
        raise ValueError("Slice is marked held_out; pass --allow-test-set to run it.")

    # Phase skips
    if slice_cfg.get("skip_eval"):
        argv.append("--skip-eval")
    if slice_cfg.get("skip_metrics"):
        argv.append("--skip-metrics")

    return argv


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch a HiL-Bench run from YAML config files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--harness",
        required=True,
        metavar="NAME_OR_PATH",
        help="Harness config name (e.g. claude) or path to a .yaml file.",
    )
    parser.add_argument(
        "--slice",
        required=True,
        metavar="NAME_OR_PATH",
        help="Slice config name (e.g. smoke, test20) or path to a .yaml file.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Override the auto-generated run-id (default: <sdk>_<slice>_<timestamp>).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the model from the harness config.",
    )
    parser.add_argument(
        "--arm",
        choices=["full_info", "neutral", "skill", "no_tool"],
        default=None,
        help="Run a single experimental arm, overriding slice modes.",
    )
    parser.add_argument(
        "--allow-test-set",
        action="store_true",
        help="Allow running a slice marked held_out: true.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the assembled command without executing it.",
    )
    args = parser.parse_args()

    harness_path = _resolve_config(args.harness, "harnesses")
    slice_path   = _resolve_config(args.slice,   "slices")

    harness_cfg = _load_yaml(harness_path)
    slice_cfg   = _load_yaml(slice_path)

    sdk = harness_cfg.get("sdk", "claude")
    run_id = args.run_id or _auto_run_id(sdk, args.slice)

    argv = build_argv(
        harness_cfg,
        slice_cfg,
        run_id,
        allow_test_set=args.allow_test_set,
        model_override=args.model,
        arm_override=args.arm,
    )

    if args.dry_run:
        print("Would run:")
        print("  " + " ".join(argv))
        return 0

    print(f"Starting run: {run_id}")
    print(f"  harness : {harness_path.name}  ({sdk})")
    print(f"  slice   : {slice_path.name}")
    print(f"  command : {' '.join(argv)}\n")

    result = subprocess.run(argv)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
