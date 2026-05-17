"""
Metrics calculation for trust_horizon HiL-SWE runs.

Reads eval_result.json and stats.json from each pass directory under a run and
computes aggregate metrics following the formulas from paper_pipeline.py:

ACCURACY (pass@k):
  pass@k = (# attempts where any of passes 1..k resolved) / (# attempts with k valid passes)

ASK METRICS — MACRO / paper (hil-bench paper_pipeline.py average-of-ratios):

  Per valid (attempt × pass), with num_blockers_resolved = unique blocker IDs answered:
    precision_for_pass = min(1.0, num_blockers_resolved / num_questions)  (0 if num_questions == 0)
    recall_for_pass    = min(1.0, num_blockers_resolved / num_blockers_total)  (0 if total == 0)
    f1_for_pass        = harmonic mean of precision_for_pass and recall_for_pass

  ask_precision = mean(precision_for_pass)
  ask_recall    = mean(recall_for_pass)
  ask_f1        = mean(f1_for_pass)

  Diagnostic micro totals (event-sum style, NOT used for primary CSV fields):
    ask_precision_event_micro, ask_recall_event_micro — sum(resolved)/sum(denominator)

  "total" questions (judge + approval + permission): same macro per-pass with num_total_questions
    as precision denominator; recall denominator unchanged.

Output files written to runs/<run_id>/metrics/:
  pass_level.json       — per-(uid, mode, pass) raw numbers
  summary.json          — per-(mode, agent) aggregated metrics

Usage:
  python3 scripts/metrics_hil_swe.py --run-id my-run
  python3 scripts/metrics_hil_swe.py --run-id my-run --passes 3
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
DATA_DIR = ROOT / "data" / "hil_bench_swe"
TASKS_DIR = DATA_DIR / "tasks"
TASKS_INDEX = DATA_DIR / "tasks_index.json"

# ── Canonical trajectory-rerun constants (run_hil_bench.py) ──────────────────
# Mirrors run_hil_bench.py lines 35-47 exactly.
TRAJECTORY_TIMEOUT_OBS_RE = re.compile(r"Command '\[.*\]' timed out after \d+ seconds")
TRAJECTORY_HICCUP_OBS = "can't answer (perhaps transient hiccup)"
TRAJECTORY_ENV_DIED_OBS = "Environment died unexpectedly"
TRAJECTORY_UNKNOWN_ERROR = "Exit due to unknown error"
# SQL-specific constants — included for completeness; won't fire for SWE tasks.
KB_QUERY_ERROR = "Error querying knowledge base"

TRAJECTORY_RERUN_OCCURRENCE_THRESHOLD_STRICT = 1   # hiccup, kb_query_error, unknown_error
TRAJECTORY_RERUN_OCCURRENCE_THRESHOLD_LENIENT = 3  # timeout

SYSTEM_ERROR_STOP_REASONS = {
    "sdk_error",
    "timeout",
    "sidecar_start_failed",
    "proxy_start_failed",
}

# Post paper-recall-fix: num_blockers_resolved = unique blocker_id count per pass.
STATS_SCHEMA_VERSION = 2


def pass_has_valid_stats_schema(pass_dir: str) -> bool:
    """True if stats.json exists and was written by schema v2 runners."""
    if not pass_dir:
        return False
    stats_path = Path(pass_dir) / "stats.json"
    if not stats_path.exists():
        return False
    try:
        stats = json.loads(stats_path.read_text())
    except Exception:
        return False
    if not isinstance(stats, dict):
        return False
    return stats.get("stats_schema_version") == STATS_SCHEMA_VERSION


def _result_has_system_error(result: dict[str, Any]) -> bool:
    """True if solve result indicates a harness/system failure.

    Applies uniformly across all SDK harnesses.
    """
    if not isinstance(result, dict):
        return False
    sdk_error = str(result.get("sdk_error") or "").strip()
    if sdk_error:
        return True
    stop_reason = str(result.get("stop_reason") or "").strip().lower()
    return stop_reason in SYSTEM_ERROR_STOP_REASONS


def _load_trajectory_steps(pass_dir: str) -> list[dict]:
    """Load trajectory steps from trajectory.json in pass_dir.

    Returns a list of {act, obs, thought?} dicts (our format), or [].
    Mirrors run_hil_bench.py load_trajectory_steps_from_dir / extract_public_trajectory_steps,
    adapted for our trajectory.json format (already in {act, obs} form).
    """
    if not pass_dir:
        return []
    traj_path = Path(pass_dir) / "trajectory.json"
    if not traj_path.exists():
        return []
    try:
        steps = json.loads(traj_path.read_text())
        return steps if isinstance(steps, list) else []
    except Exception:
        return []


def _trajectory_has_timeout_obs(steps: list[dict]) -> bool:
    """True if >= LENIENT (3) steps have a command-timeout observation.

    Mirrors run_hil_bench.py trajectory_has_timeout_obs (lines 944-950).
    """
    count = 0
    for step in steps:
        obs = step.get("obs", "")
        if isinstance(obs, str) and TRAJECTORY_TIMEOUT_OBS_RE.search(obs):
            count += 1
    return count >= TRAJECTORY_RERUN_OCCURRENCE_THRESHOLD_LENIENT


def _trajectory_has_hiccup_obs(steps: list[dict]) -> bool:
    """True if >= STRICT (1) steps have the exact ask_human hiccup observation.

    Mirrors run_hil_bench.py trajectory_has_hiccup_obs (lines 953-959).
    """
    count = 0
    for step in steps:
        obs = step.get("obs", "")
        if isinstance(obs, str) and obs.strip() == TRAJECTORY_HICCUP_OBS:
            count += 1
    return count >= TRAJECTORY_RERUN_OCCURRENCE_THRESHOLD_STRICT


def _trajectory_has_env_died_obs(steps: list[dict]) -> bool:
    """True if the LAST step's observation contains 'Environment died unexpectedly'.

    Mirrors run_hil_bench.py trajectory_has_env_died_obs (lines 962-966).
    """
    if not steps:
        return False
    obs = steps[-1].get("obs", "")
    return isinstance(obs, str) and TRAJECTORY_ENV_DIED_OBS in obs


def _trajectory_has_unknown_error(steps: list[dict]) -> bool:
    """True if the last step's 'response' field contains 'Exit due to unknown error'.

    Mirrors run_hil_bench.py trajectory_has_unknown_error (lines 969-979).
    Our trajectory.json does not emit 'response', so this will never trigger —
    but we keep it for completeness and future-proofing.
    """
    if not steps:
        return False
    last = steps[-1]
    if not isinstance(last, dict):
        return False
    response = last.get("response", "")
    return isinstance(response, str) and TRAJECTORY_UNKNOWN_ERROR in response


def _trajectory_has_kb_query_error(steps: list[dict]) -> bool:
    """True if >= STRICT (1) steps have 'Error querying knowledge base' in obs.

    Mirrors run_hil_bench.py trajectory_has_kb_query_error (lines 982-995).
    SQL-specific — will not trigger for SWE tasks in practice.
    """
    count = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        obs = step.get("obs", "")
        if isinstance(obs, str) and KB_QUERY_ERROR in obs:
            count += 1
            if count >= TRAJECTORY_RERUN_OCCURRENCE_THRESHOLD_STRICT:
                return True
    return False


def _trajectory_needs_rerun(pass_dir: str) -> bool:
    """Return True if this pass's trajectory indicates a transient failure requiring rerun.

    Mirrors run_hil_bench.py trajectory_needs_rerun (lines 1016-1024) exactly,
    adapted to read from our trajectory.json format instead of .traj files:

      trajectory_has_timeout_obs(trajectory)      — LENIENT threshold (3)
      trajectory_has_hiccup_obs(trajectory)       — STRICT threshold (1)
      trajectory_has_env_died_obs(trajectory)     — last step obs substring
      trajectory_has_unknown_error(trajectory)    — last step response substring
      trajectory_has_kb_query_error(trajectory)   — STRICT threshold (1), SQL-specific
    """
    steps = _load_trajectory_steps(pass_dir)
    # Empty trajectory (no file) → treat as infra_error, not rerun signal.
    # Only return True if the trajectory exists AND contains a specific signal.
    if not steps:
        return False
    return (
        _trajectory_has_timeout_obs(steps)
        or _trajectory_has_hiccup_obs(steps)
        or _trajectory_has_env_died_obs(steps)
        or _trajectory_has_unknown_error(steps)
        or _trajectory_has_kb_query_error(steps)
    )


# ── Row loading ─────────────────────────────────────────────────────────────

def load_pass_rows(run_dir: Path) -> list[dict[str, Any]]:
    """Walk run_dir and collect one row per (uid, mode, agent, pass_index)."""
    rows: list[dict[str, Any]] = []

    for uid_dir in sorted(run_dir.iterdir()):
        if not uid_dir.is_dir():
            continue
        uid = uid_dir.name

        for mode_dir in sorted(uid_dir.iterdir()):
            if not mode_dir.is_dir():
                continue
            mode = mode_dir.name

            for pass_dir in sorted(mode_dir.iterdir()):
                if not pass_dir.is_dir() or not pass_dir.name.startswith("pass_"):
                    continue
                try:
                    pass_idx = int(pass_dir.name[5:])
                except ValueError:
                    continue

                # Load attempt metadata (agent / model info)
                attempt_json = pass_dir / "attempt.json"
                attempt: dict[str, Any] = {}
                if attempt_json.exists():
                    try:
                        attempt = json.loads(attempt_json.read_text())
                    except Exception:
                        pass

                agent = attempt.get("harness", "unknown")
                model = attempt.get("model", "unknown")

                # Load eval result
                eval_json = pass_dir / "eval_result.json"
                eval_data: dict[str, Any] = {}
                if eval_json.exists():
                    try:
                        eval_data = json.loads(eval_json.read_text())
                    except Exception:
                        pass

                # Load trajectory stats
                stats_json = pass_dir / "stats.json"
                stats: dict[str, Any] = {}
                if stats_json.exists():
                    try:
                        stats = json.loads(stats_json.read_text())
                    except Exception:
                        pass

                # Also load result.json for basic completion status
                result_json = pass_dir / "result.json"
                result: dict[str, Any] = {}
                if result_json.exists():
                    try:
                        result = json.loads(result_json.read_text())
                    except Exception:
                        pass
                system_error_in_solve = _result_has_system_error(result)

                has_eval = bool(eval_data)
                resolved = eval_data.get("resolved") if has_eval else None

                # ── Classify eval outcome ───────────────────────────────────────────
                # Three-way: resolved | unresolved (FAIL) | infra_error (excluded from
                # pass@k).  Source of truth is the eval_status field written by
                # eval_hil_swe.py.  For legacy eval_result.json files that predate the
                # field, test_ran=False is treated as infra_error.
                eval_status_field = eval_data.get("eval_status") if has_eval else None

                if eval_status_field is not None:
                    infra_error = eval_status_field == "infra_error"
                else:
                    # Legacy format: no eval_status field.
                    infra_error = (
                        not result_json.exists()          # never ran
                        or bool(result.get("sdk_error"))  # SDK crashed
                        or (has_eval and not eval_data.get("test_ran", True))  # test patch failed
                    )
                # Any solve-time system/harness error is rerun-worthy and excluded
                # from metrics, even if an eval_result.json exists.
                infra_error = infra_error or system_error_in_solve

                row = {
                    "uid": uid,
                    "mode": mode,
                    "agent": agent,
                    "model": model,
                    "pass_index": pass_idx,
                    "status": "infra_error" if infra_error else ("resolved" if resolved else "unresolved"),
                    "resolved": resolved,
                    "num_steps": stats.get("num_steps"),
                    # clarification + elicitation (LLM judge questions, ask_human mode)
                    "num_questions": stats.get("num_questions"),
                    # approval + permission (tool-use authorization requests)
                    "num_questions_approval": stats.get("num_questions_approval"),
                    # all four types combined
                    "num_total_questions": stats.get("num_total_questions"),
                    # questions asked in full_info mode (agent asked despite having all info)
                    "num_questions_full_info": stats.get("num_questions_full_info"),
                    "num_blockers_resolved": stats.get("num_blockers_resolved"),
                    "num_blockers_total": stats.get("num_blockers_total"),
                    "num_ask_human_capped": stats.get("num_ask_human_capped"),
                    "num_ask_human_cooldown_denied": stats.get(
                        "num_ask_human_cooldown_denied"
                    ),
                    "patch_bytes": result.get("patch_bytes"),
                    "pass_dir": str(pass_dir),
                }
                rows.append(row)

    return rows


# ── Metric computation ───────────────────────────────────────────────────────

def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0


def summarize(
    rows: list[dict[str, Any]],
    expected_passes: int,
    include_partial: bool = False,
) -> dict[str, Any]:
    """Aggregate rows by (mode, agent, model) and compute pass@k + ask metrics.

    include_partial (default False, mirrors run_hil_bench.py default):
      False — only include attempts that have ALL expected_passes valid passes.
              An attempt with fewer valid passes is excluded from every pass@k.
              This is the canonical default and the scientifically correct mode.
      True  — include attempts with at least one valid pass (contributes to the
              pass@k denominators it qualifies for).  Useful for partial runs.
    """

    # Group rows by (uid, mode, agent, model) → sorted list of pass rows
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        key = (row["uid"], row["mode"], row["agent"], row["model"])
        grouped[key].append(row)

    # For each group, sort passes, filter infra errors, and filter bad trajectories.
    # Mirrors run_hil_bench.py summarize_rows lines 783-797:
    #   - Skip infra_error passes
    #   - Skip passes whose trajectory needs rerun (hiccup obs → transient judge failure)
    #   - Apply include_partial: if False (canonical default), only include attempts
    #     that completed all expected_passes valid passes.
    attempt_data: dict[tuple[str, str, str], list[list[dict]]] = defaultdict(list)
    # key = (mode, agent, model)
    for (uid, mode, agent, model), pass_rows in grouped.items():
        valid_passes = []
        for r in sorted(pass_rows, key=lambda r: r["pass_index"]):
            if r["status"] == "infra_error":
                continue
            if _trajectory_needs_rerun(r.get("pass_dir", "")):
                continue
            valid_passes.append(r)
        num_valid = len(valid_passes)
        should_include = num_valid >= 1 if include_partial else num_valid >= expected_passes
        if should_include:
            attempt_data[(mode, agent, model)].append(valid_passes)

    result: dict[str, Any] = {}
    for (mode, agent, model), attempts in sorted(attempt_data.items()):
        k_max = expected_passes
        num_attempts = len(attempts)

        num_solved_by_k = {k: 0 for k in range(1, k_max + 1)}
        num_attempts_with_k_valid = {k: 0 for k in range(1, k_max + 1)}
        # gated_pass@k: PAE-style — credit only successes where the agent
        # actually asked at least one judge question (clarification/elicitation).
        # Strips out silent-pass lucky-passes that inflate raw pass@k.
        num_gated_solved_by_k = {k: 0 for k in range(1, k_max + 1)}

        # Macro (paper) ask-metric accumulators
        precision_sum = 0.0
        recall_sum = 0.0
        f1_sum = 0.0
        precision_total_sum = 0.0
        f1_total_sum = 0.0
        ask_pass_count = 0

        # Micro totals (diagnostics only)
        total_blockers_resolved = 0.0
        # clarification + elicitation (LLM judge questions) — primary denominator
        total_questions = 0.0
        # all four types (judge + approval + permission) — alternate denominator
        total_total_questions = 0.0
        total_blockers_present = 0.0
        total_steps = 0.0
        # questions asked in full_info mode (agent asked despite having all info in prompt)
        total_questions_full_info = 0.0
        total_ask_human_capped = 0.0
        total_ask_human_cooldown_denied = 0.0
        total_attempts_and_passes = 0

        for valid_passes in attempts:
            n_valid = len(valid_passes)
            for k in range(1, k_max + 1):
                if n_valid >= k:
                    num_attempts_with_k_valid[k] += 1
            for k in range(1, n_valid + 1):
                if any(bool(valid_passes[i].get("resolved")) for i in range(k)):
                    num_solved_by_k[k] += 1
                # gated success: at least one of the first k passes both
                # resolved AND asked >= 1 judge question.
                if any(
                    bool(valid_passes[i].get("resolved"))
                    and float(valid_passes[i].get("num_questions") or 0) >= 1
                    for i in range(k)
                ):
                    num_gated_solved_by_k[k] += 1

            for row in valid_passes:
                total_attempts_and_passes += 1
                total_steps += float(row.get("num_steps") or 0)
                total_questions += float(row.get("num_questions") or 0)
                total_total_questions += float(row.get("num_total_questions") or row.get("num_questions") or 0)
                total_questions_full_info += float(row.get("num_questions_full_info") or 0)
                total_ask_human_capped += float(row.get("num_ask_human_capped") or 0)
                total_ask_human_cooldown_denied += float(
                    row.get("num_ask_human_cooldown_denied") or 0
                )

                if mode == "ask_human":
                    n_res = float(row.get("num_blockers_resolved") or 0)
                    n_q = float(row.get("num_questions") or 0)
                    n_qt = float(row.get("num_total_questions") or row.get("num_questions") or 0)
                    n_tot = float(row.get("num_blockers_total") or 0)
                    p_pass = min(1.0, n_res / n_q) if n_q > 0 else 0.0
                    r_pass = min(1.0, n_res / n_tot) if n_tot > 0 else 0.0
                    f1_pass = _f1(p_pass, r_pass)
                    p_pass_total = min(1.0, n_res / n_qt) if n_qt > 0 else 0.0
                    precision_sum += p_pass
                    recall_sum += r_pass
                    f1_sum += f1_pass
                    precision_total_sum += p_pass_total
                    f1_total_sum += _f1(p_pass_total, r_pass)
                    ask_pass_count += 1
                    total_blockers_resolved += n_res
                    total_blockers_present += n_tot

        metrics: dict[str, Any] = {
            "mode": mode,
            "agent": agent,
            "model": model,
            "num_attempts": num_attempts,
            "num_passes": k_max,
            "total_attempts_and_passes": total_attempts_and_passes,
            "avg_steps_per_pass": total_steps / total_attempts_and_passes if total_attempts_and_passes else 0.0,
            "avg_questions_per_pass": total_questions / total_attempts_and_passes if total_attempts_and_passes else 0.0,
            # avg questions asked in full_info mode per pass (non-zero only in full_info mode)
            "avg_questions_full_info_per_pass": total_questions_full_info / total_attempts_and_passes if total_attempts_and_passes else 0.0,
        }

        for k in range(1, k_max + 1):
            denom = num_attempts_with_k_valid[k]
            metrics[f"pass_at_{k}"] = num_solved_by_k[k] / denom if denom > 0 else 0.0
            metrics[f"pass_at_{k}_n"] = denom
            metrics[f"gated_pass_at_{k}"] = (
                num_gated_solved_by_k[k] / denom if denom > 0 else 0.0
            )

        if mode == "ask_human":
            # ── Primary ask metrics (paper macro): judge questions denominator
            if ask_pass_count > 0:
                ask_precision = precision_sum / ask_pass_count
                ask_recall = recall_sum / ask_pass_count
                ask_f1 = f1_sum / ask_pass_count
                ask_precision_total = precision_total_sum / ask_pass_count
                ask_recall_total = recall_sum / ask_pass_count
                ask_f1_total = f1_total_sum / ask_pass_count
            else:
                ask_precision = ask_recall = ask_f1 = 0.0
                ask_precision_total = ask_recall_total = ask_f1_total = 0.0
            metrics["ask_precision"] = ask_precision
            metrics["ask_recall"] = ask_recall
            metrics["ask_f1"] = ask_f1
            metrics["ask_precision_total"] = ask_precision_total
            metrics["ask_recall_total"] = ask_recall_total
            metrics["ask_f1_total"] = ask_f1_total
            # Diagnostic micro (legacy event-sum) — audit only
            metrics["ask_precision_event_micro"] = (
                total_blockers_resolved / total_questions if total_questions > 0 else 0.0
            )
            metrics["ask_recall_event_micro"] = (
                total_blockers_resolved / total_blockers_present if total_blockers_present > 0 else 0.0
            )
            metrics["total_questions"] = int(total_questions)
            metrics["total_ask_human_capped"] = int(total_ask_human_capped)
            metrics["total_ask_human_cooldown_denied"] = int(total_ask_human_cooldown_denied)
            metrics["total_blockers_resolved"] = int(total_blockers_resolved)
            metrics["total_blockers_present"] = int(total_blockers_present)
            metrics["total_total_questions"] = int(total_total_questions)

        if mode == "full_info":
            # Report the total count of full_info questions so analysts can see
            # how often agents asked despite having all info in their prompt.
            metrics["total_questions_full_info"] = int(total_questions_full_info)

        key = f"{mode}/{agent}/{model}"
        result[key] = metrics

    return result


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute aggregate metrics for a trust_horizon HiL-SWE run."
    )
    parser.add_argument("--run-id", required=True, help="Run identifier.")
    parser.add_argument(
        "--passes", "-k", type=int, default=3,
        help="Expected number of passes per (uid, mode) for pass@k calculation (default: 3).",
    )
    parser.add_argument("--print", action="store_true", help="Print summary to stdout.")
    parser.add_argument(
        "--include-partial",
        action="store_true",
        default=False,
        help=(
            "Include attempts that only partially completed all passes (default: False). "
            "Canonical run_hil_bench.py default is also False: only attempts with ALL "
            "expected passes valid are counted in pass@k denominators."
        ),
    )
    args = parser.parse_args()

    run_dir = RUNS_DIR / args.run_id
    if not run_dir.exists():
        print(f"ERROR: Run directory not found: {run_dir}", file=sys.stderr)
        sys.exit(1)

    rows = load_pass_rows(run_dir)
    if not rows:
        print("No pass data found in run directory.", file=sys.stderr)
        sys.exit(1)

    metrics_dir = run_dir / "metrics"
    metrics_dir.mkdir(exist_ok=True)

    # Write pass-level rows
    pass_level_path = metrics_dir / "pass_level.json"
    pass_level_path.write_text(json.dumps(rows, indent=2))
    print(f"Pass-level rows written: {pass_level_path} ({len(rows)} passes)")

    # Write summary
    summary = {
        "metadata": {
            "run_id": args.run_id,
            "num_passes": args.passes,
            "include_partial": args.include_partial,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "formula": "macro/paper (mean of per-pass min(1, resolved/total)); resolved = unique blocker IDs",
        },
        "by_mode_agent_model": summarize(
            rows, expected_passes=args.passes, include_partial=args.include_partial
        ),
    }
    summary_path = metrics_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"Summary written: {summary_path}")

    if args.print:
        for key, m in summary["by_mode_agent_model"].items():
            print(f"\n=== {key} ===")
            for k in range(1, args.passes + 1):
                pa = m.get(f"pass_at_{k}")
                n = m.get(f"pass_at_{k}_n", 0)
                if pa is not None:
                    print(f"  pass@{k}: {pa:.3f}  (n={n})")
            if m.get("ask_f1") is not None:
                q  = m.get("total_questions", 0)
                qt = m.get("total_total_questions", 0)
                r  = m.get("total_blockers_resolved", 0)
                b  = m.get("total_blockers_present", 0)
                print(f"  ask (judge q={q}):  P={m.get('ask_precision',0):.3f}  R={m.get('ask_recall',0):.3f}  F1={m.get('ask_f1',0):.3f}"
                      f"  resolved={r}/{b}")
                print(f"  ask (total q={qt}): P={m.get('ask_precision_total',0):.3f}  R={m.get('ask_recall_total',0):.3f}  F1={m.get('ask_f1_total',0):.3f}")
            print(f"  avg_questions/pass: {m.get('avg_questions_per_pass', 0):.1f}")
            print(f"  avg_steps/pass:     {m.get('avg_steps_per_pass', 0):.1f}")


if __name__ == "__main__":
    main()
