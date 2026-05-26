"""
hilbench run — launch a HiL-Bench benchmark run.

Primary interface:
  - choose a harness config (`--harness`)
  - choose a target set directly (`--p-set`, `--uids`, or `--uid-file`)
  - choose an arm preset (`--arm`)

Usage:
  python3 scripts/hilbench_run.py --harness claude --p-set public --arm default
  python3 scripts/hilbench_run.py --harness codex --uids UID1 UID2 --arm full_info
  python3 scripts/hilbench_run.py --harness claude --uid-file data/hil_swe_20_attempt_test_set_uids.txt --arm default
  python3 scripts/hilbench_run.py --harness claude --p-set both --arm enhanced
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
    "antigravity": "ANTIGRAVITY_MODEL",
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


def _build_arm_settings(
    arm: str,
    *,
    sdk: str,
    skill_template: str | None,
    guidance_template: str | None,
) -> dict[str, object]:
    """Map user-facing arm presets to run_hil_swe.py arguments/env behavior."""
    if arm == "default":
        return {
            "modes": ["ask_human"],
            "with_custom_tool": False,
            "with_skill": None,
            "with_ask_guidance": None,
        }
    if arm == "full_info":
        return {
            "modes": ["full_info"],
            "with_custom_tool": False,
            "with_skill": None,
            "with_ask_guidance": None,
        }

    # arm == "enhanced"
    if not skill_template or not guidance_template:
        raise ValueError(
            "--arm enhanced requires both --skill-template and --guidance-template."
        )
    return {
        "modes": ["ask_human"],
        # custom tool only exists as a distinct toggle on these SDKs
        "with_custom_tool": sdk in {"claude", "codex", "antigravity"},
        "with_skill": skill_template,
        "with_ask_guidance": guidance_template,
    }


def _auto_run_id(sdk: str, target_name: str) -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = Path(target_name).stem
    return f"{sdk}_{stem}_{ts}"


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


def build_argv(
    harness: dict,
    run_spec: dict,
    run_id: str,
    *,
    model_override: str | None = None,
) -> list[str]:
    """Translate harness + run-spec dicts into run_hil_swe.py argv."""
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
    uids = run_spec.get("uids")
    uids_file = run_spec.get("uids_file")
    p_set = run_spec.get("p_set")
    if uids_file:
        argv += ["--uids"] + _load_uids_file(str(uids_file))
    elif uids:
        argv += ["--uids"] + [str(u) for u in uids]
    elif p_set:
        argv += ["--p-set", str(p_set)]
    else:
        raise ValueError("Run target must specify one of: uids, uid-file, or p-set.")

    # Arm preset -> modes + enhancement flags
    arm_name = str(run_spec.get("arm") or "default")
    arm_settings = _build_arm_settings(
        arm_name,
        sdk=sdk,
        skill_template=run_spec.get("skill_template"),
        guidance_template=run_spec.get("guidance_template"),
    )
    argv += ["--modes"] + [str(m) for m in arm_settings["modes"]]
    if arm_settings["with_custom_tool"]:
        argv.append("--with-custom-tool")
    if arm_settings["with_skill"]:
        argv += ["--with-skill", str(arm_settings["with_skill"])]
    if arm_settings["with_ask_guidance"]:
        argv += ["--with-ask-guidance", str(arm_settings["with_ask_guidance"])]

    # Passes
    passes = run_spec.get("passes")
    if passes is not None:
        argv += ["--passes", str(passes)]

    # Workers
    workers = run_spec.get("workers")
    if workers is not None:
        argv += ["--workers", str(workers)]

    # Max steps
    max_steps = run_spec.get("max_steps")
    if max_steps is not None:
        argv += ["--max-steps", str(max_steps)]

    # Phase skips
    if run_spec.get("skip_eval"):
        argv.append("--skip-eval")
    if run_spec.get("skip_metrics"):
        argv.append("--skip-metrics")

    return argv


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch a HiL-Bench run from harness config + run target flags.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--harness",
        required=True,
        metavar="NAME_OR_PATH",
        help="Harness config name (e.g. claude) or path to a .yaml file.",
    )
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "--p-set",
        choices=["public", "private", "both"],
        help="Run all ingested tasks from a partition.",
    )
    target_group.add_argument(
        "--uids",
        nargs="+",
        metavar="UID",
        help="Run a specific list of task UIDs.",
    )
    target_group.add_argument(
        "--uid-file",
        "--uid_file",
        dest="uid_file",
        metavar="PATH",
        help="Text file with one UID per line (# comments allowed).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Override the auto-generated run-id.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the model from the harness config.",
    )
    parser.add_argument(
        "--arm",
        choices=["default", "enhanced", "full_info"],
        default="default",
        help=(
            "Preset arm: "
            "default=ask_human, "
            "enhanced=ask_human + skill + guidance (+ custom tool where supported), "
            "full_info=full_info mode."
        ),
    )
    parser.add_argument(
        "--skill-template",
        default="examples_ask_human_skill",
        help="Template basename for --arm enhanced (expects src/hil_swe/templates/<name>.md).",
    )
    parser.add_argument(
        "--guidance-template",
        default="examples_ask_human_guidance",
        help="Template basename for --arm enhanced (expects src/hil_swe/templates/<name>.txt).",
    )
    parser.add_argument(
        "--passes",
        type=int,
        default=3,
        help="Number of passes per task (default: 3).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Max concurrent solve workers.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the assembled command without executing it.",
    )
    args = parser.parse_args()

    harness_path = _resolve_config(args.harness, "harnesses")
    harness_cfg = _load_yaml(harness_path)
    sdk = harness_cfg.get("sdk", "claude")
    run_target_name = args.p_set or "uids"
    run_id = args.run_id or _auto_run_id(sdk, str(run_target_name))

    run_spec: dict[str, object] = {}
    if args.p_set:
        run_spec["p_set"] = args.p_set
    elif args.uid_file:
        run_spec["uids_file"] = args.uid_file
    else:
        run_spec["uids"] = args.uids or []

    # CLI choices define run behavior.
    run_spec["arm"] = args.arm
    run_spec["skill_template"] = args.skill_template
    run_spec["guidance_template"] = args.guidance_template
    run_spec["passes"] = args.passes
    if args.workers is not None:
        run_spec["workers"] = args.workers

    argv = build_argv(
        harness_cfg,
        run_spec,
        run_id,
        model_override=args.model,
    )

    if args.dry_run:
        print("Would run:")
        print("  " + " ".join(argv))
        return 0

    print(f"Starting run: {run_id}")
    print(f"  harness : {harness_path.name}  ({sdk})")
    if args.p_set:
        print(f"  target  : p-set {args.p_set}")
    elif args.uid_file:
        print(f"  target  : uid-file {args.uid_file}")
    else:
        print(f"  target  : {len(args.uids or [])} explicit uid(s)")
    print(f"  command : {' '.join(argv)}\n")

    result = subprocess.run(argv)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
