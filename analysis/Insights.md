# HiL-Dynamics: Insights from Running HiL-Bench Across Modern Harnesses

**TL;DR.** Frontier coding agents pass 75-80% of HiL-Bench tasks when given complete information. The same agents collapse to under 10% the moment 3-5 critical facts are withheld and they are forced to ask. That gap doesn't close with stronger harnesses, but it does respond, asymmetrically, to skill engineering: Codex jumps from 7% to 53% pass@3 with a tuned skill, while Claude Code's best tuning only takes it from 3% to 15%. HiL-Dynamics is the diagnostic we built to make those differences visible.

## Three headline findings

1. **The judgment gap survives modern scaffolding.** Stronger harnesses haven't taught agents *when* to ask. Every system we tested loses 70+ percentage points of pass@3 the moment information is withheld.
2. **Skill engineering is a real handle, but a harness-specific one.** Tuning the skill spec lifts Codex from 7% to 53% pass@3. Claude Code's best tuning only takes it from 3% to 15%. The template that excels on one harness can flatten or hurt another.
3. **Every `{harness, model}` has its own failure shape.** Their optimal scaffolds diverge. There is no universal recipe.

## Background: the judgment gap

Earlier this year we released **HiL-Bench**, a benchmark that asks a simple question: when a coding agent runs into something it cannot figure out on its own, does it know to ask? The headline result was a wide one. Given complete context, frontier agents passed around 80% of tasks. Strip out 3-5 critical pieces of information and hand the agent an `ask_human()` tool, and pass@3 collapsed to around 30%. The paper named the missing skill **selective escalation**: recognizing mid-task that a gap cannot be closed through exploration alone, and surfacing it before charging ahead with an assumption.

Those original results came from the SWE-agent harness. Since then the agentic surface area has exploded: Claude Code, Codex, Antigravity, ADK, OpenCode, each with its own native question-asking affordances and its own opinions about when escalation is appropriate. Selective escalation is no longer a property of the model in isolation. It is a property of the whole `{model, harness, skill}` system. And no matter how capable that system becomes, some context will always live in a human's head: product intent, business norms, the one constraint the PM never wrote down.

Most of the field is racing toward agentic autonomy. But autonomy without trustworthiness on underspecified work just produces confident wrong answers faster. **HiL-Dynamics** is the diagnostic we built to study that trade-off. It runs HiL-Bench-style tasks across modern harnesses and dissects the trajectories: when agents explore, when they ask, what they ask, whether they recover from a bad question, and how failed runs actually end.

## What we ran

Five native harness/model setups, including two Google-backed systems:

- Claude Code SDK with Claude Opus 4.7
- Codex SDK with GPT 5.5
- Google ADK with Gemini 3.1 Pro Preview
- OpenCode with GLM 5.1
- Google Antigravity with Gemini 3.5 Flash

Each system was tested under four conditions:

| Condition | What it provides | Used in |
|---|---|---|
| `Full_Info` | Missing context supplied up front; agents do not need to ask. | Finding 1 (upper-bound control) |
| `native` | Native ask affordance only: `AskUserQuestion` (Claude Code), `requestUserInput` (Codex/Antigravity), or the custom `ask_human()` MCP for ADK/OpenCode. No skill or escalation guidance beyond harness defaults. | Findings 1, 2, 3 |
| `tool/guidance` | Native plus the shared escalation guidance package in the system prompt; Claude Code and Codex additionally get a custom `ask_human()` MCP tool. | Findings 2, 3 |
| `custom skill` | Tuned `skill` template (Claude Code and Codex only), layered on top of `tool/guidance`. | Findings 2, 3 |

The headline figure in Finding 1 focuses on `Full_Info` vs `native`. `tool/guidance` and `custom skill` appear later as scaffold-level interventions.

## Finding 1: The judgment gap survives modern scaffolds

**Selective escalation remains difficult even when the harness provides a means to ask.**

We first measured agent performance out of the box, with each harness's default system prompt. Claude Code and Codex both ship with native question-asking tools (`AskUserQuestion` and `requestUserInput`, respectively); Antigravity, ADK, and OpenCode either expose weaker native affordances or none at all. For ADK and OpenCode we supplied a custom MCP tool that mirrors HiL-Bench's original `ask_human()`.

The two SDKs with native tools (Codex and Claude Code) largely declined to use them: their scaffolds discourage or block escalation once implementation begins. The gap between *planning-phase* asking (which scaffolds tolerate) and *implementation-phase* asking (which scaffolds suppress) is, we think, the most actionable place to direct future work. Agents need to interleave exploration, planning, asking, and implementing rather than treat them as separate stages.

### 1a. Native current-gen harnesses do well with full info, poorly without it

Under `Full_Info`, every harness lands in roughly the same place: 75-80% pass@3. Strip the information out and pass rates collapse:

| Harness / Model | Full_Info pass@3 | Native AskHuman pass@3 |
|---|---:|---:|
| ADK / Gemini 3.1 Pro | 80.0% | 8.0% |
| OpenCode / GLM 5.1 | 79.3% | 0.0% |
| Codex / GPT 5.5 | 78.0% | 0.7% |
| Claude Code / Claude Opus 4.7 | 76.7% | 2.7% |
| Antigravity / Gemini 3.5 Flash | 75.0% | 2.7% |

Adding the shared tool/guidance package moves Codex (42.0%), Antigravity (34.0%), and ADK (21.3%) substantially. Claude Code (12.0%) and OpenCode (11.7%) barely move. (OpenCode's number carries a parser/submission caveat; see Finding 3d before drawing strong conclusions there.) We keep the tuned custom-skill rows out of this comparison so Finding 1 isolates generic scaffold augmentation from the skill-specific intervention in Finding 2.

![Full_Info vs AskHuman pass@3](figures/01_same_model_different_scaffold.png)
*Figure 1: Full_Info vs AskHuman pass@3 across all five harness configurations. Each row shows the same model's score under full information (right dot) versus the AskHuman-only condition (left dot).*

![Codex-only selective escalation gap](figures/15_codex_selective_escalation_gap.png)
*Figure 2: GPT 5.5 / Codex selective escalation gap across native, tool/guidance, and custom skill conditions.*

### 1b. Blocker Recall vs Ask Precision

To assess selective escalation more directly than pass@3 allows, we split HiL-Bench's Ask-F1 metric into its two components: **Blocker Recall** (how many real blockers the agent surfaced) and **Ask Precision** (how many of its questions were relevant). Together they answer two practical questions. If there is a blocker, can I trust the system to clarify? And can it finish work without bothering the user indiscriminately?

When these agents do choose to ask, they ask reasonably well. Ask Precision lands between 63% and 72% for Codex, Claude Code, and OpenCode in their native conditions. The failure is on the recall side: most systems surface fewer than 40% of the registered blockers. Tool/guidance can move recall (Codex jumps from 38.0% to 61.5%), but no system we tested makes it into the high-recall, high-precision quadrant.

> **Asking *well* is not the problem. Knowing *when* to ask is.**

This diagnostic plot also keeps the tuned custom-skill rows out of the broad comparison, so Detection vs Targeting stays aligned with the baseline and shared tool/guidance setup. The custom-skill precision/recall trade-off is handled in Finding 2.

![Detection vs targeting](figures/02_detection_targeting.png)
*Figure 3: Blocker Recall vs Ask Precision across all systems and conditions. High recall means blockers were surfaced; high precision means questions were relevant.*

These results reiterate HiL-Bench's original finding: agents still cannot reliably decide when to ask for help. Even strong modern harnesses don't close the gap.

## Finding 2: Skill engineering moves the needle, per harness

**Finding 1 is diagnostic. Finding 2 is constructive.**

In real engineering workflows nobody deploys a model alone. The model arrives wrapped in project skills, custom tools, conventions, and escalation guidance. We call that wrapper **skill-in-the-loop**, and HiL-Dynamics is designed to measure whether changes to it actually move agent behavior.

We gave Claude Code and Codex thorough escalation guidance in the system prompt, plus a custom `ask_human()` MCP tool. The hypothesis was that a custom tool would be free of the training and prompting restrictions that suppress the native tools. The results were much better: all SDKs showed significant improvement, all agents asked much more frequently, and Claude Code and Codex used the custom tool more readily than their native ones.

![Customization effects on pass@3](figures/16_custom_skill_metric_lift.png)
*Figure 4: Custom-skill pass@3 lift for Claude Code and Codex relative to their native baselines.*

| system | clean tasks | pass@1 | pass@3 | Ask Precision | Blocker Recall | ask-F1 | avg questions |
|---|---:|---:|---:|---:|---:|---:|---:|
| `Claude Opus 4.7 / Native Claude-Code + custom skill` | `150` | `10.0%` | `14.0%` | `43.6%` | `31.3%` | `36.5%` | `2.55` |
| `GPT 5.5 / Native Codex + custom skill` | `150` | `36.7%` | `51.3%` | `48.9%` | `67.0%` | `56.5%` | `4.87` |

> **Skill text is not documentation. It is a behavioral prior.** It shapes what the agent believes it is supposed to do before it reads a line of code.

### What kinds of skill techniques helped

Beyond raw performance, the sweep surfaced clear patterns in how agents respond to different skill techniques:

- **When-to-ask guidance** intuitively raised performance.
- **Emotional language**, leveraging the agents' trained desire to fulfill requests with strong framing (e.g. they would fail if they don't ask), also yielded a measurable boost.

### Six skill levers

The custom-skill sweep surfaced six non-leaking dimensions that independently steer behavior:

- **Gate.** Eligibility condition for asking. Narrow gates restrict asking to rare cases ("cannot resolve from codebase"); wide gates make asking the default when implementation details remain uncertain.
- **Mandate.** Force of the instruction to ask. Stronger mandates use language like "MUST ask" and explicit failure framing. The gate says *when*; the mandate says *with what conviction*.
- **Pre-Ask Sequence.** Silent ordering step before asking. For example: enumerate blockers, pick the highest-impact unasked blocker, then ask. Controls cognitive ordering, not wording.
- **Question Quality Scaffolding.** Definition of a good question, usually through bad/good examples or artifact anchors (file, function, schema field, test, observed behavior).
- **COMBINE.** Rule that related candidate questions about one artifact merge into a single question. Prevents burning asking opportunities on fragments of one decision.
- **Search Budget.** Cap on local exploration before asking. Constrains indefinite search and acts as a tie-breaker when the gate is wide.

### Harnesses respond to skills differently

As tunable as skills are, no single template yielded the maximal improvement from the default harness baseline across all agents. A skill that excels on one harness can even degrade performance on another. Claude Code and Codex, for example, have almost opposite asking priors, with the former being more open to asking questions.

**Closing the codebase escape hatch.** The baseline gate let agents skip asking whenever they believed the answer was inferable from the codebase ("cannot resolve it from the codebase"). We replaced that clause with a strict no-inference rule ("even implicit answers from the codebase are not good enough"). For Claude Code this lifted pass@3 by +22%, because it stopped suppressing questions it would otherwise have self-resolved. For Codex, which was already asking near its ceiling, the change did nothing. Pass@3 held flat at 0.533, confirming the escape hatch never governed Codex's asking behavior.

**Strengthening the mandate.** A second variant combined a stricter gate with explicit "MUST ask" framing and failure language ("you will fail this task if you do not ask"). Codex's pass@3 jumped +630% (0.073 to 0.533), with average questions per pass rising nearly 10x (0.5 to 4.7). Claude Code's response was far more muted: average questions per pass rose only +0.9 (vs Codex's +4.2), and pass@3 reached just 0.120, confirming that Claude modulates even strong mandate text against its own inference priors.

The takeaway is asymmetric. Codex is much more responsive to mandate and gate volume settings, while Claude Code relies more on controlling its inference escape hatch. Skill engineering should be calibrated per harness rather than applied uniformly.

Even with the best performance we could achieve through careful enhancement, all agents still leave a substantial pass@3 gap compared to their full-information ceiling.

## Finding 3: Every agent has its own failure shape

**HiL-Dynamics lets us look past aggregate pass rates and examine how an agent actually moved through an underspecified task.** Two agents can fail for completely different reasons. One may write before resolving blockers; another may correctly identify the gap but fail to validate the patch that follows.

Trajectory inspection surfaces what aggregate metrics miss. We highlight four aspects here.

### 3a. Model families have a characteristic strategy shape under a fixed harness

> **Trajectory shape is a repeatable behavioral signature, not run-to-run noise.**

Under SWE-agent, related model families show similar explore/ask/write shapes. GPT models ask early. Claude models do more early exploration before asking. Hold the harness constant and the pattern reproduces across tasks.

![SWE-agent model-family strategy](figures/13_swe_agent_model_family_strategy.svg)
*Figure 5: Action-type distributions by turn for GPT, Claude, and Gemini model families under SWE-agent.*

### 3b. The same model bends to the harness

That family-level signature is not harness-invariant. GPT 5.5 asks early under SWE-agent, shifts toward early exploration under Native Codex, and with the tuned custom skill it still explores first but asks more consistently before writing. Codex + custom skill is the extreme case: 99.8% of pass rows ask before editing, median first ask at turn 18, median first write at turn 36.

Collapsing each trajectory into a strategy bucket (asked upfront, explored then asked, wrote before asking, or never asked) makes the harness effect easy to see:

| System | Asked upfront | Explored then asked | Wrote before asking | Never asked |
|---|---:|---:|---:|---:|
| `GPT 5.5 / SWE-agent` | 70.4% | 29.6% | 0% | 0% |
| `GPT 5.5 / Native Codex` | 17.0% | 70.9% | 0.9% | 11.2% |
| `GPT 5.5 / Native Codex + tool/guidance` | 11.9% | 84.8% | 0% | 3.3% |
| `GPT 5.5 / Native Codex + custom skill` | ~0% | ~100% | ~0% | ~0% |
| `Claude Opus 4.7 / Native Claude-Code` | ~6% | ~50% | ~0% | 43.5% |
| `Claude Opus 4.7 / Native Claude-Code + custom skill` | ~0% | 59.6% | 10.2% | 29.8% |
| `GLM 5.1 / Native OpenCode` | ~0% | ~50% | ~0% | ~50% (parser caveat) |

First-ask timing shifts the same way. Claude Opus 4.7 asks later under Claude Code than under SWE-agent, for example.

![Codex strategy buckets](figures/14_codex_strategy_buckets.png)
*Figure 6: Distribution of trajectory strategies (asked upfront, explored-then-asked, wrote-before-asking, never asked) across Codex conditions.*

![GPT 5.5 trajectory strategy fingerprints](figures/17_gpt55_trajectory_strategy_fingerprints.png)
*Figure 7: GPT 5.5 action-sequence fingerprints across Native Codex, tool/guidance, custom skill, and SWE-agent.*

![Strategy buckets](figures/05_strategy_buckets.png)
*Figure 8: Cross-system strategy bucket summary across all five harnesses.*

![First ask timing](figures/08_first_ask_timing.png)
*Figure 9: First-ask timing distributions relative to first edit, across configurations.*

### 3c. Recovery after a bad first ask

Strong agents shouldn't just avoid bad questions. They should notice when a question missed the blocker, sharpen the next one, and still finish the task.

Using trace-level ask sequences, we deterministically mark the first irrelevant or incorrect `ask_human()` as `I` and a blocker resolution as `R`. For Codex we filter out MCP permission prompts, which are harness permission events rather than clarification questions.

Percentages below use first-failed-ask runs as the denominator. The table is regenerated from `data/bad_first_ask_recovery.csv`.

| System | First failed ask runs | Solved after first failed ask | Asked later relevant question | Solved after later relevant question |
|---|---:|---:|---:|---:|
| `GPT 5.5 / SWE-agent` | 135 | 15.6% | 71.1% | 15.6% |
| `GPT 5.5 / Native Codex` | 67 | 6.0% | 16.4% | 1.5% |
| `GPT 5.5 / Native Codex + tool/guidance` | 107 | 19.6% | 57.0% | 17.8% |
| `GPT 5.5 / Native Codex + custom skill` | 118 | 30.5% | 72.9% | 28.8% |
| `Claude Opus 4.7 / SWE-agent` | 158 | 5.1% | 46.8% | 4.4% |
| `Claude Opus 4.7 / Native Claude-Code` | 63 | 6.3% | 20.6% | 0.0% |
| `Claude Opus 4.7 / Native Claude-Code + tool/guidance` | 59 | 0.0% | 18.6% | 0.0% |
| `Claude Opus 4.7 / Native Claude-Code + custom skill` | 86 | 1.2% | 37.2% | 1.2% |
| `Gemini 3.1 Pro / SWE-agent` | 112 | 0.9% | 83.0% | 0.9% |
| `Gemini 3.1 Pro / Native ADK` | 102 | 5.9% | 56.9% | 5.9% |

Codex + custom skill is the cleanest positive case. After a bad first ask, it more often asks a later relevant question and more often solves after doing so. Claude Code + custom skill lifts the later-relevant-question rate but does not translate that into solves in this deterministic trace proxy.

### 3d. Failed runs end in different terminal states

Failed AskHuman trajectories terminate in deterministically distinguishable states, which lets us diagnose how systems fail before, around, or after the help-seeking step. Failures vary not only by model, but also by harness.

The custom-skill rows clarify what shifted under Codex. Among unresolved `GPT 5.5 / Native Codex + custom skill` passes, 35.5% end as patch-made/no-submit, 32.4% as local-green/hidden-red, 16.9% as visible-red-at-end, and 15.2% as weak-validation-only. These are signatures of substantive work failing to close the loop, not refusal to engage. Claude Code + custom skill looks similar (37.9% local-green/hidden-red, 35.7% patch-made/no-submit).

![Terminal evidence mix](figures/04_terminal_evidence_mix.png)
*Figure 10: Terminal-state decomposition of failed AskHuman trajectories across all systems.*

## Takeaways

In real engineering work, collaboration often means digging up information locked in someone's head and asking for clarification. Across every HiL-Dynamics experiment we ran, agents consistently struggled with that step. No matter the harness or model, selective escalation on underspecified coding tasks remains an obstacle.

But each setup balances exploration and escalation differently, and each fails in its own shape. That diversity is useful. It suggests targeted areas of improvement for the next generation of models and harnesses, and it lets practitioners pick the setup that best fits their domain. Almost all problems encountered in real engineering work are underspecified; users write vague problems and hold hidden assumptions or tribal knowledge. As a community we need to push toward agents that are not only capable of solving solo, but also of knowing when to ask for context hidden in people's heads.

The unit of analysis is the whole `{model, harness, customization}` system. Harnesses and skills change pass@3, Ask-F1, question burden, strategy shape, and terminal failure anatomy. HiL-Dynamics is meant to make those differences visible. It is a way to evaluate whether a setup is **trustworthy** (it surfaces real blockers), **judicious** (it does not pester the user indiscriminately), and **steerable** (it responds in predictable ways to scaffold or skill changes).

## Figure inventory

| # | File | Used in | Role |
|---|---|---|---|
| Figure 1 | `01_same_model_different_scaffold` | Finding 1a | Primary: performance gap exists |
| Figure 2 | `15_codex_selective_escalation_gap` | Finding 1a | Codex deep-dive |
| Figure 3 | `02_detection_targeting` | Finding 1b | Primary: blocker recall vs ask precision |
| Figure 4 | `16_custom_skill_metric_lift` | Finding 2 | Primary: constructive result |
| Figure 5 | `13_swe_agent_model_family_strategy` | Finding 3a | Primary: model-family strategy fingerprint |
| Figure 6 | `14_codex_strategy_buckets` | Finding 3b | Codex strategy detail |
| Figure 7 | `17_gpt55_trajectory_strategy_fingerprints` | Finding 3b | GPT 5.5 trajectory fingerprint |
| Figure 8 | `05_strategy_buckets` | Finding 3b | Cross-system strategy summary |
| Figure 9 | `08_first_ask_timing` | Finding 3b | First-ask timing diagnostic |
| Figure 10 | `04_terminal_evidence_mix` | Finding 3d | Terminal-state decomposition |
