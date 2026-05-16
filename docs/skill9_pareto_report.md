# Skill9 Pareto Report: HiL-SWE 20-UID Evaluation

Comprehensive comparison of Trust Horizon HiL-SWE runs on the 20-attempt test set, versus original HiL-Bench references, Alina's PR baselines ([trust_horizon#1](https://github.com/melfeki-11/trust_horizon/pull/1)), and the skill7→skill9 iteration.

**20-UID test set:** [data/hil_swe_20_attempt_test_set_uids.txt](../data/hil_swe_20_attempt_test_set_uids.txt) (same UIDs as `scripts/run_skill9_full_scale.sh`)  
**Skill9 runs:** `runs/_swe_skill9_full_{claude,codex}/`  
**Short summary:** [smoke_logs/skill9_vs_alina.md](../smoke_logs/skill9_vs_alina.md)

---

## A. Apple-to-apple metrics (20-UID test set)

### Scope and caveats

- **Apple-to-apple:** Rows using the same HiL-SWE harness on the **same 20 UIDs × 3 passes** (`claude-code` / `codex`). Canonical UID list: [data/hil_swe_20_attempt_test_set_uids.txt](../data/hil_swe_20_attempt_test_set_uids.txt).
- **Not apple-to-apple:** Row 1 (“Original HiL-Bench SWE-agent”) uses different agent labels (`claude-opus-4-7`, `gpt 5.5`, etc.) and a different stack—use only as a **historical north star**.
- **Alina baselines:** P/R from project acceptance constants (20-UID runs on PR #1 config). Pass@1/Pass@3 for Alina were not stored in-repo; CSV rows leave Pass@k blank unless backfilled from Alina's run artifacts. F1 computed as `2PR/(P+R)`.

### Claude-code

| Stage | Pass@1 | Pass@3 | Precision | Recall | F1 |
|-------|--------|--------|-----------|--------|-----|
| Original HiL-Bench (reference only) | 0.00 | 0.25 | 0.26 | 0.42 | 0.32 |
| + ask_human guidance (`*_swe`) | 0.05 | 0.20 | 0.53 | 0.19 | 0.28 |
| + ignore clause / xhigh | 0.21 | 0.21 | 0.62 | 0.14 | 0.23 |
| Skill1 (`*_swe_skill`) | 0.11 | 0.11 | 0.58 | 0.25 | 0.35 |
| Skill2 (`*_swe_skill2`) | 0.06 | 0.11 | 0.70 | 0.21 | 0.33 |
| Skill3 (`*_swe_skill3`) | 0.05 | 0.10 | 0.74 | 0.27 | 0.40 |
| **Alina custom tool** | — | — | **0.58** | **0.37** | **0.45** |
| **Alina skill + guidance** | — | — | **0.65** | **0.35** | **0.45** |
| Skill7 ABD (on Alina PR) | 0.20 | 0.30 | 0.49 | 0.74 | 0.59 |
| Skill8 HE | 0.15 | 0.25 | 0.58 | 0.59 | 0.59 |
| **Skill9 split (winner)** | **0.28** | **0.33** | **0.71** | **0.71** | **0.71** |

**Skill9 vs Alina custom:** P +0.13, R +0.34  
**Skill9 vs Alina guidance:** P +0.06, R +0.36

### Codex

| Stage | Pass@1 | Pass@3 | Precision | Recall | F1 |
|-------|--------|--------|-----------|--------|-----|
| Original HiL-Bench (reference only) | 0.30 | 0.50 | 0.45 | 0.60 | 0.52 |
| + ask_human guidance | 0.15 | 0.20 | 0.68 | 0.26 | 0.38 |
| + ignore clause / xhigh | 0.10 | 0.20 | 0.78 | 0.42 | 0.54 |
| Skill1 | 0.10 | 0.20 | 0.83 | 0.42 | 0.55 |
| Skill2 | 0.20 | 0.20 | 0.77 | 0.26 | 0.38 |
| Skill3 | 0.15 | 0.20 | 0.73 | 0.37 | 0.49 |
| **Alina custom tool** | — | — | **0.56** | **0.65** | **0.60** |
| **Alina skill + guidance** | — | — | **0.74** | **0.42** | **0.54** |
| Skill7 ABD | 0.40 | 0.60 | 0.63 | 0.96 | 0.76 |
| Skill8 HE | 0.25 | 0.50 | 0.69 | 0.80 | 0.74 |
| **Skill9 split** | **0.60** | **0.65** | **0.75** | **0.94** | **0.84** |

**Skill9 vs Alina custom:** P +0.19, R +0.29  
**Skill9 vs Alina guidance:** P +0.01, R +0.52

### Arc summary

| Transition | Claude | Codex |
|------------|--------|-------|
| Early prompt/skills | High P, low R | High P, low R |
| Skill7 ABD | Recall jump (R=0.74), P=0.49 | Recall peak (R=0.96), P=0.63 |
| Skill8 HE | Balanced ~0.58/0.59 | P↑, R↓ vs skill7 |
| **Skill9 split** | **Beats both Alina lines on P and R** | **Beats both Alina lines on P and R** |

---

## B. How external references and Claude Code patterns informed the work

### 1. Procedure-Aware Evaluation (PAE)

**Source:** Cao, H., Driouich, I., & Thomas, E. *Beyond Task Completion: Revealing Corrupt Success in LLM Agents through Procedure-Aware Evaluation.* arXiv:2603.03116 (2026). [PDF](https://arxiv.org/pdf/2603.03116) · [abs](https://arxiv.org/abs/2603.03116)

| PAE idea (paper) | Trust Horizon implementation |
|------------------|------------------------------|
| Utility ≠ procedure; “corrupt success” when task passes but procedure violates constraints (§1, §5.1; Eq. 9 gated utility) | `gated_pass@k` in `scripts/metrics_hil_swe.py` (lines 375–377, 443–446): success credited only if ≥1 judge question was asked on that pass |
| Interaction quality axis (question burden, fulfillment) | Micro **ask_precision** / **ask_recall** in `scripts/metrics_hil_swe.py` (lines 13–17, 450–454); judge router in `src/shared/human_input.mjs` |
| Multi-axis gating for deployment readiness | `scripts/acceptance_skill9.py` (lines 8–42): require P/R ≥ both Alina custom-tool and skill+guidance floors |
| Read → communicate → write consistency (tripartite actions §3.1) | Tweak **F**: `READ_BEFORE_ASK` in `src/hil_swe/ask_limits.mjs` (lines 120–126); Claude `canUseTool` calls `noteFileRead` in `src/hil_swe/run_claude.mjs` (lines 136–147, ~846) |

### 2. Vinod Krane — Agent evaluation (tools, trajectories, LLM-as-judge)

**Source:** Krane, V. “Chapter 8: Agent Evaluation for LLMs — How to Test Tools, Trajectories, and LLM-as-Judge.” Medium (2025). [Article](https://medium.com/@vinodkrane/chapter-8-agent-evaluation-for-llms-how-to-test-tools-trajectories-and-llm-as-judge-788f6f3e0d52)

| Theme (article) | Trust Horizon implementation |
|-----------------|----------------------------|
| Trajectory-sensitive judge metrics; label noise | Per-pass `stats.json` and trajectory tags in `src/hil_swe/run_claude.mjs` (`[native]` / `[custom_mcp]`) |
| Slice asks by tool channel and outcome | Trajectory `act`/`obs` tags `[native]` / `[custom_mcp]` in `src/hil_swe/run_claude.mjs` (`formatAct`, lines 325–341) |
| Per-pass attribution for cap / cooldown / BR | `computeTrajectoryStats` in `src/hil_swe/run_claude.mjs` (lines 506–575) → `stats.json`; mirrored in Codex `src/hil_swe/run_codex.mjs` |
| LLM judge as selector over registry | `createHumanInputRouter` / `askHuman` in `src/shared/human_input.mjs` (e.g. `STRICT_SELECTOR_SCHEMA` lines 21–30, `validateSelectorResult` ~503+) |

### 3. Lanham — “Why success is lying to you” (2026)

**Source:** Lanham, M. “Why Success Is Lying to You — The 2026 Guide to Evaluating AI Agents.” Substack (2026). [Post](https://micheallanham.substack.com/p/why-success-is-lying-to-you-the-2026)

| Theme (post) | Trust Horizon implementation |
|--------------|------------------------------|
| Pass@k overstates agents that patch without clarifying | `gated_pass@k` reported beside raw pass@k in `scripts/metrics_hil_swe.py` and `smoke_logs/skill9_ablation_summary.md` |
| Do not pick configs that raise P only by suppressing asks | Ablation: prefer irrelevant-first throttle **K** over blind cap; production **split** keeps Codex at high R (`scripts/run_skill9_ablation.sh` lines 66–69 vs 72–74) |

### Claude Code primitives (planned paths vs this repo)

During planning, Claude Code sources were expected at:

- `CC_prompt/` — system prompt patterns  
- `CC_docs/` — product/docs for tools  
- `CC_mcp_server/` — MCP server usage  

Those directories are **not present** under `/mnt/efs/weijunluo/trust_horizon` in this workspace. The table below maps each primitive to the **exact Trust Horizon files** that implement the same behavior.

| Claude Code primitive | Trust Horizon source file(s) |
|----------------------|------------------------------|
| Native `AskUserQuestion` | `src/hil_swe/run_claude.mjs`: `isAskUserQuestionTool` (128–130), `answerClaudeAskUserQuestion` (259–316), `canUseTool` handler (~747–795) |
| System prompt / ask guidance | `src/hil_swe/templates/ask_human_guidance.txt`; `buildAskHumanGuidance` in `src/hil_swe/constants.mjs` (272–283); appended in `run_claude.mjs` / `run_codex.mjs` |
| Custom MCP `ask_human` tool | `src/hil_swe/run_claude.mjs`: `createCustomAskHumanMcpServer` (150–257); Codex: `src/hil_swe/ask_human_mcp_bridge.mjs` + `src/hil_swe/ask_human_sidecar.mjs` |
| Agent skills (`.claude/skills/…`) | `src/hil_swe/skills.mjs` (`installClaudeSkill`, 38–40); template `src/hil_swe/templates/ask_human_skill.md` |
| `CLAUDE.md` project memory | `CLAUDE_MD_HINT` → writes per-task hint in `src/hil_swe/constants.mjs` (285+); consumed in `run_claude.mjs` / `run_codex.mjs` |
| Rich MCP tool description | `RICH_ASK_HUMAN_TOOL_DESCRIPTION_*` and `richAskHumanToolDescriptionForHarness` in `src/hil_swe/constants.mjs` (204–270) |
| TodoWrite / blocker checklist seed | `SEED_BLOCKER_TODOS` + `BLOCKER_TODOS_SEED_*` in `src/hil_swe/constants.mjs` (74–76, 277–281) |
| Env-gated experiment flags | `scripts/run_hil_swe.py` `FORWARDED_ENV_KEYS` (225–264); profile drivers `scripts/run_skill9_ablation.sh`, `scripts/run_skill9_full_scale.sh` |

---

## C. What works vs what does not

Evidence: 2-UID ablation = `smoke_logs/skill9_ablation_summary.md`; 20-UID = `runs/_swe_skill9_full_*` / `smoke_logs/skill9_vs_alina.md`.

| ID | Change | Claude | Codex | 2-UID | 20-UID | Verdict | Primary source files |
|----|--------|--------|-------|-------|--------|---------|----------------------|
| A | `SEED_BLOCKER_TODOS` | R↑ | R↑ | skill7 ABD | skill7 | **Works** | `src/hil_swe/constants.mjs` (74–76, 277–281); `scripts/aggregate_skill7_ablation.py` |
| B | `CLAUDE_MD_HINT` | R↑ | R↑ | skill7 ABD | skill7 | **Works** | `src/hil_swe/constants.mjs` (77–79, 285+); `scripts/run_hil_swe.py` (253) |
| D | `RICH_ASK_TOOL_DESC` | R↑ | R↑ | skill7 ABD | skill7 | **Works** | `src/hil_swe/constants.mjs` (204–270); `run_claude.mjs` `createCustomAskHumanMcpServer` (150–157) |
| H | `SOFTEN_CATEGORY_MANDATE` | Alone: P↓ | **Best** | skill8/9 abl. | Codex split | **Codex yes; Claude needs HE** | `constants.mjs` (84–86, 255–269); `scripts/run_skill9_ablation.sh` (67–68) |
| E | `MAX_ASKS_PER_PASS=5` | **HE** | Cuts R | skill8/9 abl. | Claude split | **Claude yes** | `src/hil_swe/ask_limits.mjs` (20–23, 141–147); `run_claude.mjs` (699–701) |
| G | `IRRELEVANT_COOLDOWN=2` | Over-prune | Over-prune | skill8 HEG | — | **Hurts recall** | `ask_limits.mjs` (25–28, 136–138); `smoke_logs/skill8_ablation_summary.md` |
| J | `BLOCKER_SCALED_CAP` | 0 BR | 0 BR | skill9 JK | Not winner | **Fails with K+L** | `ask_limits.mjs` (55–61); `scripts/run_skill9_ablation.sh` (72–74) |
| K | `IRRELEVANT_FIRST_THROTTLE` | 0 BR | 0 BR | skill9 JK | Not winner | **Fails with J+L** | `ask_limits.mjs` (34–36, 141–143) |
| L | `STOP_WHEN_BLOCKERS_RESOLVED` | 0 BR | 0 BR | skill9 JK | Not winner | **Fails** | `ask_limits.mjs` (38–40, 128–134) |
| F | `READ_BEFORE_ASK` | 0 BR | 0 BR | skill9 JKF | Not winner | **Implemented, not selected** | `ask_limits.mjs` (42–48, 120–126); `run_claude.mjs` (136–147) |
| M | Claude native-only | — | — | skill9 M | — | **Not validated 20-UID** | `scripts/run_skill9_ablation.sh` (81–85); `--with-custom-tool` in `run_hil_swe.py` (865–929) |
| **split** | Codex H; Claude HE | Pareto | Pareto | ✓ | ✓ | **Production** | `scripts/run_skill9_full_scale.sh`; `scripts/acceptance_skill9.py` |
| Infra | LiteLLM judge fallback | — | — | — | Required | **Critical** | `src/hil_swe/constants.mjs` (29–44, 46–54); `scripts/run_hil_swe.py` (891–903) |

---

## D. Claude vs Codex deep dive

### Codex (skill9 split)

- **Config:** `SOFTEN_CATEGORY_MANDATE=1` only — set in `scripts/run_skill9_full_scale.sh` (Codex branch) and `scripts/run_skill9_ablation.sh` (`split` profile, lines 67–68).
- **20-UID:** P=0.75, R=0.94, pass@1=0.60, pass@3=0.65 — `runs/_swe_skill9_full_codex/metrics/summary.json`; CSV row 35.
- **Harness:** `src/hil_swe/run_codex.mjs` — `buildAskHumanGuidance("requestUserInput")` (65); MCP via `src/hil_swe/ask_human_mcp_bridge.mjs` → `ask_human_sidecar.mjs`; skill8 tracker env forwarded (251–258).
- **Why:** Custom MCP dominates asks on Codex; soften text in `constants.mjs` improves judge match without a per-pass cap killing high-recall asks.

### Claude (skill9 split)

- **Config:** `SOFTEN_CATEGORY_MANDATE=1` + `MAX_ASKS_PER_PASS=5` — `scripts/run_skill9_full_scale.sh` (Claude branch); ablation `split` lines 67–68.
- **20-UID:** P=0.71, R=0.71, pass@1=0.28, pass@3=0.33 — `runs/_swe_skill9_full_claude/metrics/summary.json`; CSV row 34.
- **Harness:** `src/hil_swe/run_claude.mjs` — `installClaudeSkill` (604); `createCustomAskHumanMcpServer` (703); native path `answerClaudeAskUserQuestion` (755); stats via `computeTrajectoryStats` (906).
- **Why:** Claude over-asked before cap + soften; cap enforced in `ask_limits.mjs`; soften via `richAskHumanToolDescriptionForHarness` in `constants.mjs`.
- **Channels:** Both native and custom MCP routed through `createHumanInputRouter` in `src/shared/human_input.mjs` (665–673).

### Production profile

```bash
# Shared (ABD) — see scripts/run_skill9_full_scale.sh
SEED_BLOCKER_TODOS=1
CLAUDE_MD_HINT=1
RICH_ASK_TOOL_DESC=1
# plus --with-custom-tool (run_hil_swe.py)

# Codex only
SOFTEN_CATEGORY_MANDATE=1

# Claude only
SOFTEN_CATEGORY_MANDATE=1
MAX_ASKS_PER_PASS=5
```

Driver: `bash scripts/run_skill9_full_scale.sh split`  
Implementation: `scripts/run_skill9_full_scale.sh`, `scripts/run_hil_swe.py`

---

## E. Productionization checklist

1. **Canonical profile** — document env per SDK; avoid enabling J/K/L/F without re-ablation.
2. **CI** — `python3 scripts/acceptance_skill9.py` on scheduled 20-UID runs.
3. **Metrics** — report P, R, F1, pass@k, and gated_pass@k together.
4. **Ops** — use LiteLLM judge when local vLLM (`ASK_HUMAN_BASE_URL=localhost:8808`) is down; see `constants.mjs` fallback.
5. **Metrics** — Alina Pass@1/Pass@3 can be backfilled from PR run logs when available.
6. **Optional** — re-run Alina configs on same 20 UIDs for strict Pass@k parity.

### Key scripts

| Script | Purpose |
|--------|---------|
| `scripts/run_skill9_full_scale.sh` | 20-UID production profile |
| `scripts/acceptance_skill9.py` | Gate vs both Alina P/R baselines |
| `scripts/metrics_hil_swe.py` | Official metrics + gated_pass@k |
| `tests/ask_limits.test.mjs` | Cap/throttle unit tests |

---

## F. Acceptance (skill9 20-UID)

| SDK | P | R | vs Alina custom | vs Alina guidance | Both |
|-----|---|---|-----------------|-------------------|------|
| claude-code | 0.709 | 0.712 | ✓ | ✓ | ✓ |
| codex | 0.752 | 0.941 | ✓ | ✓ | ✓ |

Verified by `scripts/acceptance_skill9.py` after `runs/_swe_skill9_full_*` completion.

---

## References

### External publications and articles

1. **PAE — Procedure-Aware Evaluation**  
   Cao, H., Driouich, I., & Thomas, E. (2026). *Beyond Task Completion: Revealing Corrupt Success in LLM Agents through Procedure-Aware Evaluation.* arXiv:2603.03116.  
   - PDF: https://arxiv.org/pdf/2603.03116  
   - Abstract: https://arxiv.org/abs/2603.03116  
   - **Used for:** gated utility / corrupt-success framing (§5.1); reporting `gated_pass@k` alongside pass@k; interaction-quality metrics alignment with ask P/R.

2. **Vinod Krane — Chapter 8: Agent evaluation (tools, trajectories, LLM-as-judge)**  
   Krane, V. (2025). Medium article.  
   - https://medium.com/@vinodkrane/chapter-8-agent-evaluation-for-llms-how-to-test-tools-trajectories-and-llm-as-judge-788f6f3e0d52  
   - **Used for:** trajectory + judge diagnostics; irrelevant-rate slicing; per-ask stats in `stats.json`.

3. **Lanham — Why success is lying to you (2026)**  
   Lanham, M. (2026). Substack.  
   - https://micheallanham.substack.com/p/why-success-is-lying-to-you-the-2026  
   - **Used for:** reporting gated_pass@k with P/R; avoiding precision-only configs that suppress clarification.

4. **Alina baseline (custom tool & skill + guidance)**  
   Melfeki, A. trust_horizon PR #1.  
   - https://github.com/melfeki-11/trust_horizon/pull/1  
   - **P/R constants:** `scripts/acceptance_skill9.py` (lines 8–11), `scripts/aggregate_skill9_ablation.py` (lines 11–19); CSV rows 26–29 in [Trust Horizon Agent Performance - 20-Attempt Test Set.csv](../Trust%20Horizon%20Agent%20Performance%20-%2020-Attempt%20Test%20Set.csv).

### Claude Code source paths (planning) vs Trust Horizon implementation

The Pareto plan referenced these paths under the repo root; they were **not checked in** to this workspace:

| Planned path | Status in workspace |
|--------------|---------------------|
| `CC_prompt/` | Not found |
| `CC_docs/` | Not found |
| `CC_mcp_server/` | Not found |

Trust Horizon implements the same concepts in the files below.

### Trust Horizon source files by section

#### Section B — metrics, judge, and gating

| Topic | File path |
|-------|-----------|
| ask P/R, pass@k, gated_pass@k | `scripts/metrics_hil_swe.py` |
| Hiccup / rerun detection in metrics | `scripts/metrics_hil_swe.py` (`_trajectory_needs_rerun`, ~176–199) |
| Pareto acceptance vs Alina | `scripts/acceptance_skill9.py` |
| LLM judge router & selector | `src/shared/human_input.mjs` |
| Judge env / LiteLLM fallback | `src/hil_swe/constants.mjs` (ASK_HUMAN_BASE_URL, ASK_HUMAN_MODEL) |
| Read-before-ask gate | `src/hil_swe/ask_limits.mjs` |
#### Section B — Claude Code parity (harness)

| Topic | File path |
|-------|-----------|
| Claude harness entrypoint | `src/hil_swe/run_claude.mjs` |
| Codex harness entrypoint | `src/hil_swe/run_codex.mjs` |
| Codex MCP bridge | `src/hil_swe/ask_human_mcp_bridge.mjs` |
| Ask-human sidecar (ADK/OpenCode pattern) | `src/hil_swe/ask_human_sidecar.mjs` |
| Env flags A/B/D/H/E/G/J/K/L/F | `src/hil_swe/constants.mjs` |
| System prompt template | `src/hil_swe/templates/ask_human_guidance.txt` |
| Skill template | `src/hil_swe/templates/ask_human_skill.md` |
| Skill installer | `src/hil_swe/skills.mjs` |
| Orchestration & env forwarding | `scripts/run_hil_swe.py` |

#### Section C — ablation evidence and toggles

| Topic | File path |
|-------|-----------|
| Skill9 split ablation | `scripts/aggregate_skill9_ablation.py`, `scripts/run_skill9_ablation.sh`, `smoke_logs/skill9_ablation_summary.md` |
| Ask-limit unit tests | `tests/ask_limits.test.mjs` |

#### Section D — production profile and full-scale runs

| Topic | File path |
|-------|-----------|
| 20-UID production driver | `scripts/run_skill9_full_scale.sh` |
| Full-scale run artifacts | `runs/_swe_skill9_full_claude/`, `runs/_swe_skill9_full_codex/` |
| Short results summary | `smoke_logs/skill9_vs_alina.md` |

#### Run output schema (metrics inputs)

| Topic | File path |
|-------|-----------|
| Per-pass stats | `runs/<run_id>/<uid>/ask_human/pass_<n>/stats.json` |
| Trajectory (SWE-agent format) | `runs/<run_id>/<uid>/ask_human/pass_<n>/trajectory.json` |
| Aggregated metrics | `runs/<run_id>/metrics/summary.json` |

---

## Infra note (first 20-UID attempt)

The first skill9 full-scale run returned `can't answer` on most asks because `.env` pointed the judge at local vLLM (`localhost:8808`) with a vLLM-only model slug while vLLM was down. Fixes in `src/hil_swe/constants.mjs` (LiteLLM URL + model fallback) and `scripts/run_hil_swe.py` (`--env KEY=` clears forwarded env). Re-run succeeded with BR>0 and valid P/R.
