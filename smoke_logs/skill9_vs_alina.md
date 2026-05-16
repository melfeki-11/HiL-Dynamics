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
| Skill7 ABD (recall-only) | 0.49 / 0.74 | 0.63 / 0.96 |
| Skill8 HE | 0.58 / 0.59 | 0.69 / 0.80 |
| **Skill9 split** | **0.71 / 0.71** | **0.75 / 0.94** |

## Winning profile: `split`

- **Codex:** `SOFTEN_CATEGORY_MANDATE=1` (Tweak H only). No fixed cap, no cooldown, no scaled cap — softening alone moved Codex into both Alina envelopes.
- **Claude:** `SOFTEN_CATEGORY_MANDATE=1 MAX_ASKS_PER_PASS=5` (Tweak HE). Cap stops 51%-irrelevant skill7 over-ask without killing recall.
- ABD base flags on both: `SEED_BLOCKER_TODOS`, `CLAUDE_MD_HINT`, `RICH_ASK_TOOL_DESC`, `--with-custom-tool`.

## 2-UID ablation (selection rationale)

| Profile | Claude P/R | Codex P/R | Notes |
|---------|------------|-----------|-------|
| **split (HE / H)** | **0.70 / 0.67** | **0.76 / 0.92** | **Winner** |
| split_JK (J+K+L) | 0.00 / 0.00 | 0.00 / 0.00 | over-throttle: registry-stop + irrelevant-first cleared BR |
| split_HEKJ | 0.00 / 0.00 | 0.00 / 0.00 | adds cooldown — same over-throttle |
| split_M (Claude native only) | 0.00 / 0.00 | 0.00 / 0.00 | same JK base under the hood |
| split_JKF (read-before-ask) | 0.00 / 0.00 | 0.00 / 0.00 | gate trips before any blocker is matched |

Other profiles (Tier 1+2 combinations) are now retired in favor of `split`. Tweaks J/K/L/F/M are implemented and gated behind env flags for future use, but the 20-UID winner does not need them.

## Implementation summary (skill9)

Env-gated tracker [`src/hil_swe/skill8_ask_limits.mjs`](../src/hil_swe/skill8_ask_limits.mjs):

| Flag | Tweak | Status |
|------|-------|--------|
| `SOFTEN_CATEGORY_MANDATE` | H — softer blocker checklist + rich MCP description | shipped in skill8 |
| `MAX_ASKS_PER_PASS` | E — per-pass cap | shipped in skill8 |
| `IRRELEVANT_COOLDOWN` | G — cooldown after N consecutive irrelevant answers | shipped in skill8 |
| `BLOCKER_SCALED_CAP` | J — `min(6, num_blockers + 1)` | skill9, available |
| `IRRELEVANT_FIRST_THROTTLE` | K — cap only after first irrelevant | skill9, available |
| `STOP_WHEN_BLOCKERS_RESOLVED` | L — short-circuit when registry resolved | skill9, available |
| `READ_BEFORE_ASK` / `READ_BEFORE_ASK_MIN_FILES` | F — require Read/Grep before ask (Claude) | skill9, available |

Driver / aggregator / acceptance:

- [`scripts/run_skill9_ablation.sh`](../scripts/run_skill9_ablation.sh)
- [`scripts/aggregate_skill9_ablation.py`](../scripts/aggregate_skill9_ablation.py)
- [`scripts/run_skill9_full_scale.sh`](../scripts/run_skill9_full_scale.sh)
- [`scripts/acceptance_skill9.py`](../scripts/acceptance_skill9.py)
- [`scripts/diag_skill78_slice.py`](../scripts/diag_skill78_slice.py) → [`skill78_diag_slice.md`](skill78_diag_slice.md)
- Tests: [`tests/skill8_ask_limits.test.mjs`](../tests/skill8_ask_limits.test.mjs) — 6/6 passing.

## Infra fixes during validation

The first 20-UID attempt produced `can't answer` on ~95% of asks: `.env` set
`ASK_HUMAN_BASE_URL` to a local vLLM (`localhost:8808`) that was down, with a
vLLM-only model slug. Fixes:

1. `src/hil_swe/constants.mjs` — ignore local vLLM URLs and fall back to the
   LiteLLM proxy when local is unreachable.
2. `src/hil_swe/constants.mjs` — when the judge URL is the LiteLLM proxy,
   swap a vLLM-only model slug for `bedrock/qwen.qwen3-32b-v1:0`.
3. `scripts/run_hil_swe.py` — `--env KEY=` now clears `KEY` from the forwarded
   env (used to override `ASK_HUMAN_BASE_URL=` in profiles).

Single-pass smoke after fix: BR=4/5, P=1.00, R=0.80 on UID `698139c7…`.

## 20-UID metrics (summary)

Claude: pass@1=0.28, pass@3=0.33, P=0.71, R=0.71, F1=0.71  
Codex: pass@1=0.60, pass@3=0.65, P=0.75, R=0.94, F1=0.84
