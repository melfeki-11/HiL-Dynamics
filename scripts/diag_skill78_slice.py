#!/usr/bin/env python3
"""Diagnostic slice on skill7/skill8 full-scale runs.

Usage:
  python3 scripts/diag_skill78_slice.py
  python3 scripts/diag_skill78_slice.py --run-id _swe_skill8_full_claude

Writes smoke_logs/skill78_diag_slice.json and prints a short markdown table.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
OUT_JSON = ROOT / "smoke_logs" / "skill78_diag_slice.json"
OUT_MD = ROOT / "smoke_logs" / "skill78_diag_slice.md"

IRRELEV = "irrelevant question"
NATIVE_RE = re.compile(r"ask_human \[native\]", re.I)
MCP_RE = re.compile(r"ask_human \[custom_mcp\]", re.I)


def load_summary(run_id: str) -> dict | None:
    p = RUNS / run_id / "metrics" / "summary.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    for _k, m in data.get("by_mode_agent_model", {}).items():
        if str(_k).startswith("ask_human/"):
            return m
    return None


def slice_run(run_id: str) -> dict:
    run_dir = RUNS / run_id
    m = load_summary(run_id) or {}
    bp = int(m.get("total_blockers_present") or 0)
    br = int(m.get("total_blockers_resolved") or 0)
    q_official = int(m.get("total_questions") or 0)

    traj_asks = 0
    traj_irrelev = 0
    native_asks = 0
    native_irrelev = 0
    mcp_asks = 0
    mcp_irrelev = 0
    per_uid: dict[str, dict] = defaultdict(
        lambda: {"q": 0, "irrelev": 0, "br": 0, "bt": 0, "passes": 0}
    )

    for traj_path in sorted(run_dir.rglob("trajectory.json")):
        uid = traj_path.parts[-4]
        stats_path = traj_path.parent / "stats.json"
        st = {}
        if stats_path.exists():
            st = json.loads(stats_path.read_text())
        per_uid[uid]["passes"] += 1
        per_uid[uid]["br"] += int(st.get("num_blockers_resolved") or 0)
        per_uid[uid]["bt"] += int(st.get("num_blockers_total") or 0)

        steps = json.loads(traj_path.read_text())
        for step in steps:
            act = str(step.get("act") or "")
            if "ask_human" not in act:
                continue
            obs = str(step.get("obs") or "")
            traj_asks += 1
            per_uid[uid]["q"] += 1
            if IRRELEV in obs:
                traj_irrelev += 1
                per_uid[uid]["irrelev"] += 1
            if NATIVE_RE.search(act):
                native_asks += 1
                if IRRELEV in obs:
                    native_irrelev += 1
            elif MCP_RE.search(act):
                mcp_asks += 1
                if IRRELEV in obs:
                    mcp_irrelev += 1

    p_official = br / q_official if q_official else 0.0
    q_eff = q_official - traj_irrelev if q_official else 0
    # Upper bound: irrelevant asks contribute 0 to BR
    p_upper = br / max(q_eff, 1) if q_official else 0.0

    return {
        "run_id": run_id,
        "official": {
            "precision": p_official,
            "recall": br / bp if bp else 0.0,
            "total_questions": q_official,
            "blockers_resolved": br,
            "blockers_present": bp,
            "capped": int(m.get("total_ask_human_capped") or 0),
            "cooldown_denied": int(m.get("total_ask_human_cooldown_denied") or 0),
        },
        "trajectory": {
            "ask_steps": traj_asks,
            "irrelevant_steps": traj_irrelev,
            "irrelevant_rate": traj_irrelev / traj_asks if traj_asks else 0.0,
            "precision_upper_bound": min(p_upper, 1.0),
        },
        "by_channel": {
            "native": {"asks": native_asks, "irrelevant": native_irrelev},
            "custom_mcp": {"asks": mcp_asks, "irrelevant": mcp_irrelev},
        },
        "per_uid": dict(per_uid),
    }


def render_md(rows: list[dict]) -> str:
    lines = [
        "# skill7/8 diagnostic slice",
        "",
        "| run | P | R | Q | BR | irrelev% | P upper | native asks | mcp asks |",
        "|-----|---|---|---|----|---------|---------|-------------|----------|",
    ]
    for r in rows:
        o = r["official"]
        t = r["trajectory"]
        ch = r["by_channel"]
        lines.append(
            f"| {r['run_id']} | {o['precision']:.3f} | {o['recall']:.3f} | "
            f"{o['total_questions']} | {o['blockers_resolved']} | "
            f"{100*t['irrelevant_rate']:.1f}% | {t['precision_upper_bound']:.3f} | "
            f"{ch['native']['asks']} | {ch['custom_mcp']['asks']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", action="append", default=[])
    args = parser.parse_args()

    default_runs = [
        "_swe_skill7_full_claude",
        "_swe_skill8_full_claude",
        "_swe_skill7_full_codex",
        "_swe_skill8_full_codex",
    ]
    run_ids = args.run_id or default_runs
    rows = []
    for rid in run_ids:
        if (RUNS / rid).exists():
            rows.append(slice_run(rid))
        else:
            print(f"WARN: missing {rid}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(rows, indent=2))
    OUT_MD.write_text(render_md(rows))
    print(render_md(rows))
    print(f"Wrote {OUT_JSON} and {OUT_MD}")


if __name__ == "__main__":
    main()
