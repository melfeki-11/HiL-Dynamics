# Skill9: How the references and Claude Code shaped the work

This is my write-up of why skill9’s **`split`** profile works, how it compares to [Alina’s PR #1](https://github.com/melfeki-11/trust_horizon/pull/1), and which ideas from outside the repo actually made it into the harness.

**Eval setup:** 20 UIDs × 3 passes on the same HiL-SWE stack. Canonical UIDs: [`data/hil_swe_20_attempt_test_set_uids.txt`](../data/hil_swe_20_attempt_test_set_uids.txt). Numbers: [`docs/skill9_pareto_report.md`](skill9_pareto_report.md), [`smoke_logs/skill9_vs_alina.md`](../smoke_logs/skill9_vs_alina.md).

---

## What I took from the three references

### PAE ([arXiv:2603.03116](https://arxiv.org/pdf/2603.03116))

PAE argues that an agent can fix the repo and still fail the *procedure*—skipping human clarification or asking in ways the benchmark doesn’t intend. That matched how we already score ask precision and recall against the blocker registry.

I used it in three practical ways:

- Treat ask P/R as real metrics, not a footnote to pass@k.
- Report **gated_pass@k** so a “pass” doesn’t count if the agent never seriously used the human loop (`scripts/metrics_hil_swe.py`).
- Gate releases on beating **both** of Alina’s baselines, not just one (`scripts/acceptance_skill9.py`).

### Krane ([Chapter 8](https://medium.com/@vinodkrane/chapter-8-agent-evaluation-for-llms-how-to-test-tools-trajectories-and-llm-as-judge-788f6f3e0d52))

Krane’s piece pushed me to read trajectories, not only aggregate P/R. After we got recall up, a large share of Claude’s questions were still labeled `irrelevant question`—the agent was asking, but the judge wasn’t buying the phrasing.

That’s why I added native vs MCP tags in `run_claude.mjs` and per-pass `stats.json`. The takeaway wasn’t “ask less everywhere”; it was “fix wording and don’t apply the same throttle to Codex and Claude.”

### Lanham ([2026 post](https://micheallanham.substack.com/p/why-success-is-lying-to-you-the-2026))

Lanham’s point is simple: pass@k can look fine while the agent avoids real clarification. Our early runs already showed high P / low R; later runs showed high R / lower P. I didn’t want a config that wins by going quiet.

So I report pass@k, P, R, F1, and gated pass together, and I picked the production profile only when it beat **both** Alina lines on **both** P and R—with **different** env for Claude vs Codex in `split`, because they use the ask tools differently.

---

## How Claude Code reference material helped

I used three local reference trees (`CC_prompt/`, `CC_docs/`, `CC_mcp_server/`) as maps to how Claude Code is *supposed* to work—not as code we ship. They helped me check that Trust Horizon mirrors the product shape: layered system prompt, per-tool instructions, project memory, native `AskUserQuestion`, plus optional MCP.

From **`CC_prompt/10-context-and-prompts.md`** I aligned our global ask procedure with `templates/ask_human_guidance.txt` and `buildAskHumanGuidance` in `constants.mjs`, wired into `run_claude.mjs`.

From **`CC_docs/tools.md`** and **`subsystems.md`** I matched AskUserQuestion interception in `canUseTool`, TodoWrite-style blocker checklist at turn 1, `CLAUDE.md`-style workspace hints, and rich text on the custom MCP tool so the model sees “read first, one specific question” at the tool boundary—not only in the system prompt.

From **`CC_prompt/11-mcp-integration.md`** and **`CC_mcp_server/README.md`** I kept Alina’s pattern: native + custom MCP, both routed through the same judge in `human_input.mjs`, with Codex going through `ask_human_mcp_bridge.mjs`.

---

## What actually works (production `split` profile)

### What Alina already shipped

Alina’s [PR #1](https://github.com/melfeki-11/trust_horizon/pull/1) gave us the right skeleton: skill + guidance, custom MCP `ask_human`, native `AskUserQuestion` routing, and the shared LLM judge. I built skill9 on top of that; I didn’t replace it.

### What I added that moved the 20-UID numbers

These are the pieces that stayed in the winning config:

1. **Blocker checklist at turn 1** — nudges the agent to walk the same categories as the registry. Recall on Claude went from Alina’s ~0.35–0.37 band to ~0.71.

2. **Workspace clarification hint** — a light per-task memory nudge so long SWE runs don’t forget to keep clarifying.

3. **Rich `ask_human` tool description** — read-first, one question, tied to a concrete identifier in the code. Fewer vague questions, better judge match.

4. **Softer wording on category coverage** — categories are lenses to discover blockers, not a rigid “you must ask five times” mandate. Precision came up without telling the agent to stop asking.

5. **A per-pass ask cap on Claude only (5)** — after softening, Claude still over-asked; a small cap cut junk asks. Codex is **not** capped, so recall stays ~0.94.

6. **Split env by SDK** — Codex runs soften-only; Claude runs soften + cap. One global cap would have hurt Codex the way Alina’s guidance did.

7. **Eval plumbing** — gated_pass@k, full-scale and ablation scripts, acceptance check against both Alina P/R floors.

### How to reproduce

```bash
bash scripts/run_skill9_full_scale.sh split
```

Shared: blocker checklist seed, workspace hint, rich MCP description, `--with-custom-tool`.  
Codex: soften category wording only.  
Claude: soften wording + max 5 asks per pass.

---

## 20-UID results

| | Claude P | Claude R | Codex P | Codex R |
|---|---------:|---------:|--------:|--------:|
| Alina custom tool | 0.58 | 0.37 | 0.56 | 0.65 |
| Alina skill + guidance | 0.65 | 0.35 | 0.74 | 0.42 |
| **Skill9 split** | **0.67** | **0.64** | **0.78** | **0.91** |

Skill9 is the first row that beats **both** Alina configs on **both** P and R for both SDKs. Pass@k moved up too (Claude pass@3 0.33; Codex pass@3 0.65).

---

## How this differs from Alina’s version

Alina fixed the hard problem: Trust Horizon behaves like Claude Code for human input—two ask paths, skill, guidance, one judge. The gap in her numbers was familiar: guidance could raise P but crush R on Codex (0.65 → 0.42), and Claude barely reached the registry on recall (R ≤ 0.37) even with the custom tool.

I didn’t need another wall of guidance text. I needed scaffolding so asks line up with the registry, and calibration so “precision” doesn’t mean “stop asking.”

| | Alina | Skill9 split |
|---|--------|----------------|
| Ask paths | Native + MCP + skill/guidance | Same |
| Claude recall | Low | ~0.64 (capped-macro proxy) |
| Codex recall | Drops under guidance | ~0.91 (capped-macro proxy) |
| Precision vs recall | Tradeoff between her two baselines | Strong on both axes |
| Config | One story for both SDKs | Different env per SDK |

---

## Main harness files (if you’re reviewing the PR)

- `src/hil_swe/run_claude.mjs` — native ask, custom MCP, trajectory stats  
- `src/hil_swe/constants.mjs` — guidance, tool text, env-gated behavior  
- `src/hil_swe/ask_limits.mjs` — per-pass cap (Claude in production)  
- `src/hil_swe/skills.mjs`, `templates/ask_human_*`  
- `src/shared/human_input.mjs` — judge  
- `scripts/run_skill9_full_scale.sh`, `scripts/acceptance_skill9.py`

---

## Bottom line

Alina gave us the Claude Code-shaped harness. PAE told me to score the procedure, not just the patch. Krane told me to read trajectories when P and R pull apart. Lanham told me not to pick a quiet agent and call it precise. The CC folders helped me see where product behavior lives in prompt, tool text, memory, and MCP—and I mirrored that in Trust Horizon.

The stack that worked is Alina’s base plus checklist seed, workspace hint, rich and softened tool copy, and a Claude-only cap in a **split** profile. On the 20-UID set that’s roughly **0.67 / 0.64** on Claude and **0.78 / 0.91** on Codex (capped-macro P/R from event-count stats; upper-bound proxy vs paper unique-blocker recall), above both of her reference lines.

---

## See also

- [skill9_pareto_report.md](skill9_pareto_report.md) — full metric tables  
- [PR #3](https://github.com/melfeki-11/trust_horizon/pull/3) — skill9 implementation on `main`
