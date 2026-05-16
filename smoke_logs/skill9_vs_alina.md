# Skill9 vs Alina baselines

Full write-up: [docs/skill9_pareto_report.md](../docs/skill9_pareto_report.md)

## Bottom line (20-UID, 3 passes)

| SDK | Skill9 split P | Skill9 split R | Beats custom | Beats guidance | Both |
|-----|---:|---:|:---:|:---:|:---:|
| claude-code | **0.709** | **0.712** | Y | Y | **Y** |
| codex | **0.752** | **0.941** | Y | Y | **Y** |

Both SDKs beat **both** Alina baselines on **both** precision and recall.

## Reference baselines

| Baseline | Claude P/R | Codex P/R |
|----------|------------|-----------|
| Alina custom tool | 0.58 / 0.37 | 0.56 / 0.65 |
| Alina skill + guidance | 0.65 / 0.35 | 0.74 / 0.42 |
| **Skill9 split** | **0.71 / 0.71** | **0.75 / 0.94** |

## Winning profile: `split`

- **Codex:** `SOFTEN_CATEGORY_MANDATE=1` only (no per-pass cap).
- **Claude:** `SOFTEN_CATEGORY_MANDATE=1` and `MAX_ASKS_PER_PASS=5`.
- **Both:** `SEED_BLOCKER_TODOS=1`, `CLAUDE_MD_HINT=1`, `RICH_ASK_TOOL_DESC=1`, plus `--with-custom-tool`.

## 2-UID ablation (selection rationale)

See [`smoke_logs/skill9_ablation_summary.md`](skill9_ablation_summary.md). Winner: **`split`** (Codex soften-only; Claude soften + cap of 5).

## Scripts

- [`scripts/run_skill9_full_scale.sh`](../scripts/run_skill9_full_scale.sh) — 20-UID driver
- [`scripts/run_skill9_ablation.sh`](../scripts/run_skill9_ablation.sh) — 2-UID profiles
- [`scripts/aggregate_skill9_ablation.py`](../scripts/aggregate_skill9_ablation.py)
- [`scripts/acceptance_skill9.py`](../scripts/acceptance_skill9.py)
- [`tests/skill8_ask_limits.test.mjs`](../tests/skill8_ask_limits.test.mjs)

Harness: [`src/hil_swe/skill8_ask_limits.mjs`](../src/hil_swe/skill8_ask_limits.mjs) (per-pass cap and optional guards).

## 20-UID metrics

Claude: pass@1=0.28, pass@3=0.33, P=0.71, R=0.71, F1=0.71  
Codex: pass@1=0.60, pass@3=0.65, P=0.75, R=0.94, F1=0.84

UIDs: [`data/hil_swe_20_attempt_test_set_uids.txt`](../data/hil_swe_20_attempt_test_set_uids.txt)
